"""
Тестер доступности сервисов.

Две основные проверки:
  1. check_site() — TLS-хендшейк + чтение части ответа. Ловит RST-блокировку И
     «заморозку» на 16-20 КБ (характерная подпись DPI-троттлинга), если запросить
     достаточно данных и они не докачиваются за таймаут.
  2. stun_rtt() — UDP-замер задержки через STUN. Используется как лёгкий индикатор
     здоровья UDP-пути (тот же принцип применяет монитор голосового Discord).
"""
from __future__ import annotations

import os
import socket
import ssl
import struct
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

# Порог «докачки», выше которого считаем, что заморозки 16-20 КБ нет.
FREEZE_PROBE_BYTES = 24_000
# Сервисы, у которых важен голос (UDP). Для них скоринг требует живого UDP-пути.
VOICE_SERVICES = {"discord"}
# Выше этого RTT (мс) считаем UDP-путь задушенным (голос будет рваться).
VOICE_RTT_MAX_MS = 700.0
# Доля потерянных мелких STUN-проб, выше которой путь считаем негодным для голоса.
VOICE_LOSS_MAX = 0.4
# Размер «большого» STUN-запроса (байт) — эмулирует видео-пакет демонстрации экрана.
# Если мелкие проходят, а такие теряются — путь давится на больших UDP.
VOICE_BIG_BYTES = 1000
# Цели по сервисам (host, путь для GET). Путь выбран так, чтобы отдавалось тело.
DEFAULT_TARGETS: dict[str, list[tuple[str, str]]] = {
    "discord": [
        ("discord.com", "/"),
        ("gateway.discord.gg", "/"),
        ("cdn.discordapp.com", "/"),
    ],
    "youtube": [
        ("www.youtube.com", "/"),
        ("i.ytimg.com", "/"),
        ("redirector.googlevideo.com", "/"),
    ],
}

STUN_SERVERS = [
    ("stun.l.google.com", 19302),
    ("stun1.l.google.com", 19302),
]


@dataclass
class SiteResult:
    host: str
    ok: bool = False
    status: str = ""          # OK | RST | TIMEOUT | DNS | FREEZE | ERR
    latency_ms: float = -1.0  # время до первого байта ответа
    bytes_read: int = 0
    detail: str = ""


@dataclass
class ServiceResult:
    service: str
    sites: list[SiteResult] = field(default_factory=list)
    # Здоровье UDP-пути (голос). None — у сервиса нет голоса (напр. youtube);
    # True/False — прошла ли UDP/STUN-проверка. Голос Discord живёт по UDP, и
    # без этой проверки стратегия могла «пройти» с открытым сайтом, но мёртвым
    # голосом — ровно тот баг, что ловил пользователь.
    voice_ok: bool | None = None
    voice_rtt: float = -1.0
    voice_loss: float = 0.0   # доля потерянных мелких проб 0..1 (предиктор долгого коннекта)
    voice_jitter: float = 0.0 # разброс RTT, мс (нестабильность → рвущийся/долгий коннект)
    voice_conf: str = ""      # "high" | "low" — уверенность авто-проверки голоса
    voice_detail: str = ""    # человекочитаемые метрики (loss/jitter/big) для логов

    def voice_score(self) -> float:
        """Штраф качества голоса (меньше = лучше/быстрее коннектится). Только для
        живого голоса; потери весят больше всего — именно они дают долгий вход в войс."""
        if not self.voice_ok:
            return 0.0
        rtt = self.voice_rtt if self.voice_rtt > 0 else 300.0
        return rtt * 0.1 + self.voice_loss * 300.0 + self.voice_jitter * 0.3

    @property
    def sites_ok(self) -> bool:
        # Половина целей (округл. вверх) должна открыться.
        good = sum(1 for s in self.sites if s.ok)
        return good >= (len(self.sites) + 1) // 2

    @property
    def ok(self) -> bool:
        # Сервис рабочий, если открыт сайт И (если применимо) жив голос по UDP.
        if self.voice_ok is None:
            return self.sites_ok
        return self.sites_ok and self.voice_ok

    @property
    def avg_latency_ms(self) -> float:
        good = [s.latency_ms for s in self.sites if s.ok and s.latency_ms >= 0]
        return sum(good) / len(good) if good else -1.0


def check_site(
    host: str,
    path: str = "/",
    port: int = 443,
    timeout: float = 5.0,
    probe_freeze: bool = True,
) -> SiteResult:
    """Проверяет доступность сайта через TLS + чтение части ответа."""
    res = SiteResult(host=host)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # нас интересует проходимость, не валидность серта

    t0 = time.perf_counter()
    sock = None
    try:
        raw = socket.create_connection((host, port), timeout=timeout)
        raw.settimeout(timeout)
        sock = ctx.wrap_socket(raw, server_hostname=host)
    except socket.gaierror:
        res.status = "DNS"
        res.detail = "не резолвится DNS"
        return res
    except (ConnectionResetError, ssl.SSLError) as e:
        res.status = "RST"
        res.detail = str(e)[:120]
        return res
    except socket.timeout:
        res.status = "TIMEOUT"
        res.detail = "таймаут при подключении/хендшейке"
        return res
    except OSError as e:
        res.status = "ERR"
        res.detail = str(e)[:120]
        return res

    try:
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            "User-Agent: Mozilla/5.0\r\n"
            "Accept: */*\r\n"
            "Connection: close\r\n\r\n"
        ).encode()
        sock.sendall(req)

        first_byte_at = -1.0
        total = 0
        while True:
            try:
                chunk = sock.recv(16384)
            except socket.timeout:
                break
            except (ConnectionResetError, ssl.SSLError):
                # Обрыв посреди чтения — троттлинг/сброс.
                break
            if not chunk:
                break
            if first_byte_at < 0:
                first_byte_at = time.perf_counter()
                res.latency_ms = round((first_byte_at - t0) * 1000, 1)
            total += len(chunk)
            if not probe_freeze or total >= FREEZE_PROBE_BYTES:
                break

        res.bytes_read = total
        if total == 0:
            res.status = "RST"
            res.detail = "хендшейк прошёл, но тело не пришло"
            return res

        # Заморозка 16-20 КБ: получили немного и застряли, не добрав порога.
        if probe_freeze and total < 16_000:
            elapsed = time.perf_counter() - first_byte_at
            if elapsed >= timeout * 0.8:
                res.status = "FREEZE"
                res.detail = f"докачано {total} б и застряло (~16-20КБ DPI)"
                return res

        res.ok = True
        res.status = "OK"
        return res
    except OSError as e:
        res.status = "ERR"
        res.detail = str(e)[:120]
        return res
    finally:
        try:
            if sock:
                sock.close()
        except Exception:
            pass


def stun_rtt(server: tuple[str, int] | None = None, timeout: float = 2.0) -> float | None:
    """Возвращает RTT (мс) до STUN-сервера по UDP или None при неудаче."""
    servers = [server] if server else STUN_SERVERS
    # STUN Binding Request: type=0x0001, len=0, magic cookie, 12-байт transaction id
    txid = os.urandom(12)
    packet = struct.pack("!HHI", 0x0001, 0x0000, 0x2112A442) + txid
    for host, port in servers:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(timeout)
        try:
            addr = (socket.gethostbyname(host), port)
            t0 = time.perf_counter()
            s.sendto(packet, addr)
            data, _ = s.recvfrom(2048)
            rtt = (time.perf_counter() - t0) * 1000
            if len(data) >= 20 and data[4:8] == b"\x21\x12\xa4\x42":
                return round(rtt, 1)
        except (socket.timeout, OSError):
            continue
        finally:
            s.close()
    return None


def _stun_packet(pad: int = 0) -> tuple[bytes, bytes]:
    """STUN Binding Request. Возвращает (txid, packet).

    pad>0 добивает пакет comprehension-optional атрибутом (тип >=0x8000, сервер
    его игнорирует) до нужного размера — так проверяем прохождение БОЛЬШИХ UDP
    (аналог видео-пакета демонстрации экрана), не ломая ответ STUN.
    """
    txid = os.urandom(12)
    attrs = b""
    if pad > 0:
        val = b"\x00" * (pad - (pad % 4))  # длина атрибута кратна 4
        attrs = struct.pack("!HH", 0x8022, len(val)) + val  # 0x8022 — опциональный
    packet = struct.pack("!HHI", 0x0001, len(attrs), 0x2112A442) + txid + attrs
    return txid, packet


def stun_burst(server: tuple[str, int], count: int, timeout: float,
               pad: int = 0) -> tuple[int, list[float]]:
    """Шлёт count STUN-запросов НА ОДНОМ сокете и собирает ответы в окне timeout.

    Так за ~timeout меряем сразу потери и джиттер (а не timeout*count).
    Возвращает (сколько отправлено, список RTT ответивших по txid).
    """
    try:
        addr = (socket.gethostbyname(server[0]), server[1])
    except OSError:
        return 0, []
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    sent: dict[bytes, float] = {}
    answered: set[bytes] = set()
    rtts: list[float] = []
    try:
        for _ in range(count):
            txid, packet = _stun_packet(pad)
            sent[txid] = time.perf_counter()
            try:
                s.sendto(packet, addr)
            except OSError:
                pass
            time.sleep(0.02)  # лёгкий разгон, не заваливаем одним всплеском
        deadline = time.perf_counter() + timeout
        # Ждём, пока не ответят ВСЕ отправленные или не выйдет окно (не выходим раньше).
        while len(answered) < count and time.perf_counter() < deadline:
            try:
                s.settimeout(max(0.05, deadline - time.perf_counter()))
                data, _ = s.recvfrom(2048)
            except (socket.timeout, OSError):
                break
            if len(data) >= 20 and data[4:8] == b"\x21\x12\xa4\x42":
                rxid = data[8:20]
                if rxid in sent and rxid not in answered:
                    answered.add(rxid)
                    rtts.append(round((time.perf_counter() - sent[rxid]) * 1000, 1))
    finally:
        s.close()
    return count, rtts


def check_voice(attempts: int = 5, timeout: float = 1.6) -> ServiceResult:
    """Многометричная авто-проверка UDP-пути голоса/демонстрации Discord.

    Меряет не только RTT, а связку сигналов (всё в фоне, без участия человека):
      - потери мелких STUN-проб (задушенный/рвущийся путь),
      - джиттер (нестабильность → рвущийся голос),
      - прохождение БОЛЬШОГО пакета ~1 КБ (аналог видео демонстрации экрана).

    Возвращает ServiceResult с заполненными voice_ok/voice_rtt/voice_conf/voice_detail
    (используем как контейнер метрик; service="_voice").

    Оговорка сохраняется: STUN — прокси UDP-пути, а не сам медиасервер Discord.
    Но многометрика ловит большинство случаев «STUN пингуется, а медиа мёртвая»
    (высокие потери / джиттер / провал больших пакетов). Пороги калибруются по логам.
    """
    out = ServiceResult(service="_voice")
    # Берём первый STUN-сервер, который вообще отвечает на мелкие пробы.
    small_rtts: list[float] = []
    used = None
    for srv in STUN_SERVERS:
        _, small_rtts = stun_burst(srv, count=attempts, timeout=timeout, pad=0)
        if small_rtts:
            used = srv
            break
    if not used or not small_rtts:
        out.voice_ok, out.voice_rtt = False, -1.0
        out.voice_conf, out.voice_detail = "high", "UDP мёртв: STUN не отвечает"
        return out

    loss = 1.0 - len(small_rtts) / max(1, attempts)
    best = min(small_rtts)
    jitter = round(max(small_rtts) - min(small_rtts), 1) if len(small_rtts) > 1 else 0.0
    # Большой пакет — на том же сервере, что ответил на мелкие (изолируем путь от сервера).
    _, big_rtts = stun_burst(used, count=3, timeout=timeout, pad=VOICE_BIG_BYTES)
    big_ok = len(big_rtts) >= 1

    out.voice_rtt = best
    out.voice_loss = round(loss, 3)
    out.voice_jitter = jitter
    small_ok = loss <= VOICE_LOSS_MAX and best <= VOICE_RTT_MAX_MS
    out.voice_ok = bool(small_ok and big_ok)
    # Уверенность: high — сигнал чёткий; low — пограничный (тут уместен опц. ручной чек).
    borderline = (loss > VOICE_LOSS_MAX * 0.6 or best > VOICE_RTT_MAX_MS * 0.7
                  or jitter > 250 or (small_ok and not big_ok))
    out.voice_conf = "low" if borderline else "high"
    out.voice_detail = (f"rtt={best}мс loss={loss:.0%} jitter={jitter}мс "
                        f"big={'ok' if big_ok else 'DROP'}({len(big_rtts)}/3)")
    return out


def test_service(
    service: str,
    targets: list[tuple[str, str]] | None = None,
    timeout: float = 5.0,
    probe_freeze: bool = True,
    check_voice_udp: bool = True,
) -> ServiceResult:
    tgts = targets if targets is not None else DEFAULT_TARGETS.get(service, [])
    result = ServiceResult(service=service)
    if not tgts:
        return result
    # Цели проверяем параллельно — так один прогон занимает ~таймаут, а не сумму.
    with ThreadPoolExecutor(max_workers=len(tgts)) as ex:
        futures = [
            ex.submit(check_site, host, path, timeout=timeout, probe_freeze=probe_freeze)
            for host, path in tgts
        ]
        result.sites = [f.result() for f in futures]
    # Для голосовых сервисов дополнительно меряем UDP-путь (многометрично).
    if check_voice_udp and service in VOICE_SERVICES:
        vc = check_voice(timeout=min(1.6, timeout))
        result.voice_ok = vc.voice_ok
        result.voice_rtt = vc.voice_rtt
        result.voice_loss = vc.voice_loss
        result.voice_jitter = vc.voice_jitter
        result.voice_conf = vc.voice_conf
        result.voice_detail = vc.voice_detail
    return result


def test_all(
    services: list[str] | None = None,
    timeout: float = 5.0,
    probe_freeze: bool = True,
) -> list[ServiceResult]:
    svcs = services or list(DEFAULT_TARGETS.keys())
    # Сервисы проверяем параллельно (один winws обслуживает оба) — прогон
    # занимает ~таймаут самого долгого, а не сумму. Порядок сохраняем.
    with ThreadPoolExecutor(max_workers=max(1, len(svcs))) as ex:
        futures = [
            ex.submit(test_service, s, timeout=timeout, probe_freeze=probe_freeze)
            for s in svcs
        ]
        return [f.result() for f in futures]
