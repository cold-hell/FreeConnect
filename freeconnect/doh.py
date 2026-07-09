"""
DNS-over-HTTPS для FreeConnect.

Часть блокировок в РФ — DNS-уровневые: провайдер подменяет ответы DNS (spoofing),
и обход DPI не помогает, потому что домен резолвится в «неправильный» IP. DoH шифрует
DNS-запросы к доверенному резолверу (Cloudflare) — провайдер их не видит и не подменяет.

Реализация без внешних сервисов и стороннего софта:
  - локальный DNS-прокси на 127.0.0.1:53 (UDP), внутри процесса приложения;
  - каждый запрос как есть (RFC 8484, application/dns-message) уходит POST'ом к DoH по IP
    (Google 8.8.8.8/8.8.4.4, затем Cloudflare 1.1.1.1/1.0.0.1 — чтобы не зависеть от DNS
    для самого DoH и переживать сбой одного провайдера);
  - ответ возвращается клиенту.

DNS активного адаптера переключаем на 127.0.0.1 с ЗАПАСНЫМ 8.8.8.8: если прокси вдруг
умрёт (или приложение жёстко убьют), Windows сам сходит на 8.8.8.8 напрямую — интернет
не пропадёт. На выключении DNS возвращаем на «автоматически» (DHCP).

Идея подсмотрена у B4 (перенаправление DNS в DoH).
"""
from __future__ import annotations

import atexit
import os
import socket
import struct
import subprocess
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

_NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW — не мигать консолью PowerShell

# DoH-резолверы по IP (без бутстрап-DNS для самого DoH). Серты Google/Cloudflare
# содержат IP-SAN (8.8.8.8/1.1.1.1 и т.д.) — проверка TLS проходит по умолчанию.
# Google первым: он верифицируется даже в урезанных CA-хранилищах; Cloudflare — фолбэком.
# Если ни один не верифицируется/не отвечает, self_test не даст включить DoH (DNS не трогаем).
_DOH_URLS = [
    "https://8.8.8.8/dns-query",
    "https://8.8.4.4/dns-query",
    "https://1.1.1.1/dns-query",
    "https://1.0.0.1/dns-query",
]
_FALLBACK_DNS = "8.8.8.8"  # запасной прямой (plaintext) резолвер на адаптере


# ---------------------------------------------------------------- DNS wire ---
def _build_query(domain: str, qtype: int = 1) -> bytes:
    """Минимальный DNS-запрос (RD=1, один вопрос). qtype 1 = A."""
    tid = os.urandom(2)
    header = tid + b"\x01\x00" + struct.pack("!HHHH", 1, 0, 0, 0)
    qname = b"".join(bytes([len(p)]) + p.encode() for p in domain.split(".")) + b"\x00"
    return header + qname + struct.pack("!HH", qtype, 1)  # QCLASS IN


def _response_ok(resp: bytes | None) -> bool:
    """Ответ валиден: заголовок на месте, RCODE=0 (NOERROR) и есть хотя бы один ответ."""
    if not resp or len(resp) < 12:
        return False
    flags = struct.unpack("!H", resp[2:4])[0]
    ancount = struct.unpack("!H", resp[6:8])[0]
    return (flags & 0x0F) == 0 and ancount >= 1


def _doh_query(raw: bytes, timeout: float = 5.0) -> bytes | None:
    """Шлёт сырой DNS-пакет в DoH и возвращает сырой ответ (или None)."""
    for url in _DOH_URLS:
        try:
            req = urllib.request.Request(
                url, data=raw, method="POST",
                headers={
                    "Content-Type": "application/dns-message",
                    "Accept": "application/dns-message",
                    "User-Agent": "FreeConnect-DoH",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception:
            continue
    return None


# ------------------------------------------------------------ локальный прокси ---
class DoHProxy:
    """UDP-сервер 127.0.0.1:53, пересылающий запросы в DoH."""

    def __init__(self, host: str = "127.0.0.1", port: int = 53,
                 log: Callable[[str], None] | None = None) -> None:
        self.host = host
        self.port = port
        self._log = log or (lambda *_: None)
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._pool: ThreadPoolExecutor | None = None
        self._stop = threading.Event()

    def start(self) -> bool:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((self.host, self.port))
            s.settimeout(0.5)
        except OSError as e:
            self._log(f"DoH: не удалось занять {self.host}:{self.port} ({e})")
            return False
        self._sock = s
        self._stop.clear()
        self._pool = ThreadPoolExecutor(max_workers=16)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return True

    def _serve(self) -> None:
        assert self._sock and self._pool
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            self._pool.submit(self._handle, data, addr)

    def _handle(self, data: bytes, addr) -> None:
        resp = _doh_query(data)
        if resp and self._sock:
            try:
                self._sock.sendto(resp, addr)
            except OSError:
                pass

    def self_test(self) -> bool:
        """Проверяет, что upstream DoH реально отвечает (самая ненадёжная часть)."""
        return _response_ok(_doh_query(_build_query("cloudflare.com"), timeout=6.0))

    def stop(self) -> None:
        self._stop.set()
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
        if self._thread:
            self._thread.join(timeout=2)
        if self._pool:
            self._pool.shutdown(wait=False)
        self._sock = self._thread = self._pool = None


# --------------------------------------------------- смена DNS адаптера (netsh) ---
def _ps(script: str, timeout: float = 15.0) -> tuple[int, str]:
    try:
        p = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=timeout, creationflags=_NO_WINDOW,
        )
        return p.returncode, (p.stdout or "").strip()
    except Exception as e:  # noqa: BLE001
        return 1, str(e)


def get_active_adapter() -> str | None:
    """InterfaceAlias адаптера с IPv4-шлюзом по умолчанию (тот, что в интернете)."""
    rc, out = _ps(
        "(Get-NetIPConfiguration | Where-Object {$_.IPv4DefaultGateway -ne $null} "
        "| Select-Object -First 1 -ExpandProperty InterfaceAlias)"
    )
    return out.splitlines()[0].strip() if (rc == 0 and out) else None


def get_dns(alias: str) -> list[str]:
    # Одинарные кавычки: PowerShell трактует как литерал, а subprocess не мучается с
    # экранированием двойных кавычек. Алиас может содержать пробелы (Ethernet 2, Wi-Fi).
    rc, out = _ps(
        f"(Get-DnsClientServerAddress -InterfaceAlias '{alias}' "
        f"-AddressFamily IPv4).ServerAddresses -join ','"
    )
    return [s for s in out.split(",") if s] if rc == 0 else []


def set_dns(alias: str, servers: list[str]) -> bool:
    joined = ",".join(f"'{s}'" for s in servers)
    rc, out = _ps(
        f"Set-DnsClientServerAddress -InterfaceAlias '{alias}' -ServerAddresses ({joined})"
    )
    if rc != 0:
        return False
    _ps("Clear-DnsClientCache")  # сбросить закешированные (возможно, подменённые) записи
    return True


def reset_dns(alias: str) -> bool:
    rc, _ = _ps(f"Set-DnsClientServerAddress -InterfaceAlias '{alias}' -ResetServerAddresses")
    _ps("Clear-DnsClientCache")
    return rc == 0


# ------------------------------------------------------------------ менеджер ---
class DoHManager:
    """Оркестрирует DoH: прокси -> self-test -> смена DNS адаптера, и надёжный откат."""

    # Не давать pywebview рекурсивно обходить менеджер (Lock/socket/пул) при сборке js_api.
    _serializable = False

    def __init__(self, log: Callable[[str], None] | None = None) -> None:
        self._log = log or (lambda *_: None)
        self._proxy: DoHProxy | None = None
        self._adapter: str | None = None
        self._active = False
        self._lock = threading.Lock()
        atexit.register(self.stop)  # бэкстоп на нормальный выход интерпретатора

    @property
    def active(self) -> bool:
        return self._active

    def start(self) -> bool:
        with self._lock:
            if self._active:
                return True
            proxy = DoHProxy(log=self._log)
            if not proxy.start():
                return False
            if not proxy.self_test():
                self._log("DoH: upstream 1.1.1.1 недоступен — откат")
                proxy.stop()
                return False
            adapter = get_active_adapter()
            if not adapter:
                self._log("DoH: активный адаптер не найден — откат")
                proxy.stop()
                return False
            prev = get_dns(adapter)
            if not set_dns(adapter, ["127.0.0.1", _FALLBACK_DNS]):
                self._log("DoH: не удалось сменить DNS адаптера — откат")
                proxy.stop()
                return False
            self._proxy, self._adapter, self._active = proxy, adapter, True
            self._log(f"DoH включён на «{adapter}» (было: {prev or 'DHCP'})")
            return True

    def stop(self) -> None:
        with self._lock:
            if not self._active and not self._proxy:
                return
            if self._adapter:
                try:
                    reset_dns(self._adapter)
                    self._log(f"DoH выключен, DNS адаптера «{self._adapter}» -> авто")
                except Exception as e:  # noqa: BLE001
                    self._log(f"DoH: ошибка отката DNS: {e}")
            if self._proxy:
                self._proxy.stop()
            self._proxy = self._adapter = None
            self._active = False
