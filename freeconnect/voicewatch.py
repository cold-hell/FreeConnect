"""
Пассивный детектор живости голоса Discord (эксперим.).

Почему так, а не STUN и не «залогин»: надёжно измерить голос Discord со стороны
нельзя — RTC-сервер отвечает только на пакеты авторизованной сессии, а STUN-прокси
([[freeconnect-voice-false-positive]]) даёт ложные срабатывания. Зато можно НАБЛЮДАТЬ
реальный медиапоток пользователя: его клиент уже залогинен и шлёт/принимает UDP к
голосовому серверу. На Windows удалённый адрес UDP-сокета в таблицах не виден, поэтому
берём поток через WinDivert в режиме SNIFF (только КОПИИ пакетов, без дропа) —
отдельным хендлом, движок winws не трогаем.

Сигнал смерти: мыШлём медиа (исходящих пакетов много), а входящих от того же пира
нет несколько окон подряд — голос односторонний/мёртвый (ТСПУ душит UDP или проблема
RTC-сервера, см. [[freeconnect-voice-5000ms-region]]). Тогда зовём on_dead — выше это
триггерит авто-восстановление и подсказку сменить регион канала.

Любая ошибка инициализации/чтения = тихо выключаемся и логируем. Обход не страдает.
"""
from __future__ import annotations

import threading
import time
from typing import Callable

# Discord-голос: медиа-серверы слушают высокие UDP-порты. Локальный порт клиента тоже
# обычно высокий, но нам достаточно, что ХОТЯ БЫ один конец потока >= этого порога —
# так отсекаем массу низкопортового UDP (DNS/NTP/mDNS и пр.), не привязываясь к
# конкретным (меняющимся) диапазонам IP Discord.
VOICE_PORT_MIN = 50000

# Флаги/слои WinDivert 2.x (см. WinDivert.h)
_LAYER_NETWORK = 0
_FLAG_SNIFF = 0x0001        # копируем пакеты, не изымаем из стека
_FLAG_RECV_ONLY = 0x0004    # только чтение (=READ_ONLY); НЕ 0x0008 (то SEND_ONLY)
_INVALID_HANDLE = -1


def parse_ipv4(packet: bytes) -> tuple[str, str, int] | None:
    """(src_ip, dst_ip, proto) из IPv4-пакета, или None если это не IPv4/битый."""
    if len(packet) < 20 or (packet[0] >> 4) != 4:
        return None
    proto = packet[9]
    src = ".".join(str(b) for b in packet[12:16])
    dst = ".".join(str(b) for b in packet[16:20])
    return src, dst, proto


def udp_ports(packet: bytes) -> tuple[int, int] | None:
    """(src_port, dst_port) для UDP поверх IPv4 (учитывая длину IP-заголовка)."""
    if len(packet) < 20 or (packet[0] >> 4) != 4:
        return None
    ihl = (packet[0] & 0x0F) * 4
    if packet[9] != 17 or len(packet) < ihl + 8:
        return None
    sp = (packet[ihl] << 8) | packet[ihl + 1]
    dp = (packet[ihl + 2] << 8) | packet[ihl + 3]
    return sp, dp


def is_voice_udp(packet: bytes) -> bool:
    """UDP и хотя бы один порт голосовой (>= VOICE_PORT_MIN)."""
    ports = udp_ports(packet)
    if not ports:
        return False
    return ports[0] >= VOICE_PORT_MIN or ports[1] >= VOICE_PORT_MIN


def remote_ip(packet: bytes, outbound: bool) -> str | None:
    """IP пира (голосового сервера): для исходящего — назначение, для входящего — источник."""
    info = parse_ipv4(packet)
    if not info:
        return None
    src, dst, _ = info
    return dst if outbound else src


def verdict(out_pkts: int, in_pkts: int, min_out: int, dead_in: int) -> str:
    """Оценка окна по одному пиру:
    - 'idle' — исходящего медиа мало, человек не в звонке → не тревожим;
    - 'dead' — медиа шлём (out >= min_out), а входящего почти нет (in <= dead_in) →
      голос односторонний/мёртвый;
    - 'ok'  — поток двусторонний."""
    if out_pkts < min_out:
        return "idle"
    if in_pkts <= dead_in:
        return "dead"
    return "ok"


def pick_peer(peers: dict[str, list[int]]) -> str | None:
    """Пир с наибольшим суммарным трафиком в окне (активный голосовой сервер)."""
    if not peers:
        return None
    return max(peers, key=lambda ip: peers[ip][0] + peers[ip][1])


class VoiceWatch:
    """Фоновый SNIFF-наблюдатель. on_dead(reason) вызывается, когда голос устойчиво
    односторонний. Всё внутри best-effort: сбой → self.ok=False и выход из потока."""

    _serializable = False   # чтобы pywebview не обходил объект при сборке js_api

    def __init__(
        self,
        on_dead: Callable[[str], None],
        dll_path: str | None = None,
        window: float = 5.0,
        min_out: int = 25,
        dead_in: int = 3,
        need_dead_windows: int = 2,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.on_dead = on_dead
        self.dll_path = dll_path
        self.window = window
        self.min_out = min_out
        self.dead_in = dead_in
        self.need_dead_windows = need_dead_windows
        self._log = log or (lambda _m: None)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._handle = None
        self._divert = None
        self.ok = False

    # ---- жизненный цикл ----
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="voicewatch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._close()   # разблокирует висящий WinDivertRecv (вернёт ошибку → выход)

    # ---- WinDivert через ctypes ----
    def _open(self) -> None:
        import ctypes
        from ctypes import wintypes

        dll = self.dll_path
        if not dll:
            from . import paths
            dll = str(paths.BIN_DIR / "WinDivert.dll")
        self._divert = ctypes.WinDLL(dll)
        d = self._divert
        d.WinDivertOpen.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_int16, ctypes.c_uint64]
        d.WinDivertOpen.restype = wintypes.HANDLE
        d.WinDivertRecv.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_uint,
                                    ctypes.POINTER(ctypes.c_uint), ctypes.c_void_p]
        d.WinDivertRecv.restype = wintypes.BOOL
        d.WinDivertClose.argtypes = [wintypes.HANDLE]
        d.WinDivertClose.restype = wintypes.BOOL

        # Ловим только голосовой UDP; SNIFF+RECV_ONLY = чистое наблюдение, без дропа/инжекта.
        flt = f"udp and (udp.SrcPort >= {VOICE_PORT_MIN} or udp.DstPort >= {VOICE_PORT_MIN})"
        handle = d.WinDivertOpen(flt.encode("ascii"), _LAYER_NETWORK, 0,
                                 _FLAG_SNIFF | _FLAG_RECV_ONLY)
        if handle is None or handle == _INVALID_HANDLE or handle == wintypes.HANDLE(-1).value:
            err = ctypes.get_last_error()
            raise OSError(f"WinDivertOpen failed (err={err})")
        self._handle = handle

    def _recv(self):
        """Один пакет: (packet_bytes, outbound_bool) либо None при ошибке/закрытии."""
        import ctypes
        buf = (ctypes.c_ubyte * 65535)()
        # WINDIVERT_ADDRESS 2.x = 64 байта; нам нужен флаг Outbound (бит 17 слова после
        # 8-байтного Timestamp): Layer[0:8] Event[8:16] Sniffed[16] Outbound[17] ...
        addr = (ctypes.c_ubyte * 64)()
        recv_len = ctypes.c_uint(0)
        ok = self._divert.WinDivertRecv(self._handle, ctypes.cast(buf, ctypes.c_void_p),
                                        65535, ctypes.byref(recv_len), ctypes.byref(addr))
        if not ok:
            return None
        n = recv_len.value
        packet = bytes(buf[:n])
        word = int.from_bytes(bytes(addr[8:12]), "little")
        outbound = bool((word >> 17) & 1)
        return packet, outbound

    def _close(self) -> None:
        try:
            if self._handle is not None and self._divert is not None:
                self._divert.WinDivertClose(self._handle)
        except Exception:  # noqa: BLE001
            pass
        self._handle = None

    # ---- основной цикл ----
    def _run(self) -> None:
        try:
            self._open()
            self.ok = True
            self._log("voicewatch: SNIFF запущен")
        except Exception as e:  # noqa: BLE001
            self.ok = False
            self._log(f"voicewatch: не запустился (ок, выключаюсь): {e}")
            return
        peers: dict[str, list[int]] = {}
        dead_streak = 0
        window_end = time.monotonic() + self.window
        try:
            while not self._stop.is_set():
                got = self._recv()
                if got is None:
                    break                      # хендл закрыт (stop) или ошибка чтения
                packet, outbound = got
                if is_voice_udp(packet):
                    ip = remote_ip(packet, outbound)
                    if ip:
                        slot = peers.setdefault(ip, [0, 0])
                        slot[0 if outbound else 1] += 1
                now = time.monotonic()
                if now >= window_end:
                    peer = pick_peer(peers)
                    if peer:
                        out_p, in_p = peers[peer]
                        v = verdict(out_p, in_p, self.min_out, self.dead_in)
                        if v == "dead":
                            dead_streak += 1
                            if dead_streak >= self.need_dead_windows:
                                dead_streak = 0
                                self._log(f"voicewatch: голос мёртв (out={out_p} in={in_p} пир={peer})")
                                try:
                                    self.on_dead(f"нет входящего голоса от {peer}")
                                except Exception:  # noqa: BLE001
                                    pass
                        else:
                            dead_streak = 0
                    peers = {}
                    window_end = now + self.window
        except Exception as e:  # noqa: BLE001
            self._log(f"voicewatch: цикл упал (выключаюсь): {e}")
        finally:
            self.ok = False
            self._close()
