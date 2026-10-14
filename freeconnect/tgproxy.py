"""
Обход блокировки Telegram — локальный MTProto-прокси поверх WebSocket.

Ядро — проект [Flowseal/tg-ws-proxy](https://github.com/Flowseal/tg-ws-proxy) (MIT),
вендорен в `freeconnect/tgws` без изменений (см. `tgws/UPSTREAM.md`). Этот модуль —
наш тонкий адаптер: раскладывает настройки FreeConnect по конфигу ядра, поднимает его
в отдельном потоке с собственным event loop, отдаёт ссылку для Telegram и умеет
останавливать. Своей сетевой логики тут нет намеренно: всё, что касается блокировок,
резервных Cloudflare-доменов и мультиплексирования медиа, живёт в ядре и обновляется
вместе с ним.

Как это работает (кратко):
1. Ядро слушает 127.0.0.1 и притворяется MTProto-прокси (`tg://proxy?...&secret=dd…`).
2. Telegram шлёт туда свой обычный обфусцированный MTProto-поток.
3. Ядро расшифровывает его, определяет дата-центр и перешифровывает поток в
   WebSocket к веб-входу Telegram (`kws{dc}.web.telegram.org/apiws`) — то есть наружу
   уходит обычный HTTPS к легальному домену, а не характерный почерк MTProto.
4. Если прямой адрес дата-центра недоступен — идёт резерв через Cloudflare-домены;
   медиа-потоки мультиплексируются по HTTP/2.

Прав администратора не требует, работает независимо от winws и sing-box.
"""
from __future__ import annotations

import asyncio
import logging
import socket
import threading
from typing import Callable, Iterable, Mapping

from . import paths

DEFAULT_PORT = 1443           # порт по умолчанию у ядра (и у upstream)
LOCAL_HOST = "127.0.0.1"
# Живые адреса веб-входа в обход DNS-блокировки: DNS отдаёт заблокированные адреса,
# поэтому ходим на конкретный IP, подставляя правильный SNI. Один узел обслуживает
# ДЦ2 и ДЦ4. Переопределяется из config.json (tg_dc_ips) без пересборки.
DEFAULT_DC_IPS: dict[int, str] = {2: "149.154.167.220", 4: "149.154.167.220"}

_CORE_LOGGER = "tg-mtproto-proxy"
_START_TIMEOUT = 30.0         # сек на подъём (внутри ядро прогревает пул WS)
_STOP_TIMEOUT = 25.0


class TgProxyError(Exception):
    """Прокси Telegram не удалось поднять или остановить."""


# ---------------------------------------------------------------------------
# Доступ к вендоренному ядру
# ---------------------------------------------------------------------------

def _core():
    """Импортирует ядро по требованию: без него модуль всё равно импортируется,
    а GUI и тесты не падают на отсутствующих зависимостях."""
    from .tgws import config as core_config
    from .tgws import tg_ws_proxy as core_module
    return core_config, core_module


def available() -> bool:
    try:
        _core()
        return True
    except Exception:  # noqa: BLE001  — нет зависимостей/файлов ядра
        return False


def core_version() -> str:
    """Версия вендоренного ядра (из его же `__init__.py`)."""
    try:
        from .tgws import __version__ as version
        return str(version)
    except Exception:  # noqa: BLE001
        return "unknown"


# ---------------------------------------------------------------------------
# Вспомогательное: порт и ссылка
# ---------------------------------------------------------------------------

def port_is_free(port: int, host: str = LOCAL_HOST) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, int(port)))
        except OSError:
            return False
    return True


def pick_port(preferred: int, host: str = LOCAL_HOST) -> int:
    """Предпочитаем заданный порт, но если он занят (например, у пользователя уже
    запущен отдельный TgWsProxy) — берём любой свободный, чтобы не падать.
    Значение <= 0 означает «выбери любой свободный»."""
    preferred = int(preferred)
    if preferred > 0 and port_is_free(preferred, host):
        return preferred
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def deeplink(port: int = DEFAULT_PORT, host: str = LOCAL_HOST, secret: str = "",
             fake_tls_domain: str = "") -> str:
    """Ссылка автонастройки. Telegram сам предложит добавить прокси.

    `dd<secret>` — обычный MTProto-секрет; `ee<secret><hex домена>` — режим Fake TLS,
    когда локальный участок маскируется под TLS к указанному домену (нужен редко,
    например если прокси не на localhost)."""
    host = host or LOCAL_HOST
    port = int(port or DEFAULT_PORT)
    secret = (secret or "").strip()
    if not secret:
        return f"tg://proxy?server={host}&port={port}"
    domain = (fake_tls_domain or "").strip()
    if domain:
        return (f"tg://proxy?server={host}&port={port}"
                f"&secret=ee{secret}{domain.encode('ascii', 'ignore').hex()}")
    return f"tg://proxy?server={host}&port={port}&secret=dd{secret}"


def normalize_dc_ips(raw: Mapping | None) -> dict[int, str]:
    """Приводит {'2': '1.2.3.4'} из конфига к {2: '1.2.3.4'}, отбрасывая мусор."""
    result: dict[int, str] = {}
    if not raw:
        return dict(DEFAULT_DC_IPS)
    for key, value in dict(raw).items():
        try:
            dc = int(key)
        except (TypeError, ValueError):
            continue
        ip = str(value or "").strip()
        if not ip:
            continue
        try:
            socket.inet_aton(ip)
        except OSError:
            continue
        result[dc] = ip
    return result or dict(DEFAULT_DC_IPS)


class _CallbackHandler(logging.Handler):
    """Пробрасывает записи ядра в наш лог, чтобы всё было в одном месте."""

    def __init__(self, sink: Callable[[str], None]) -> None:
        super().__init__(level=logging.INFO)
        self._sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._sink(f"tgws: {record.getMessage()}")
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Сам прокси
# ---------------------------------------------------------------------------

class TgProxy:
    """Один экземпляр на процесс: ядро upstream держит настройки в глобальном
    объекте, поэтому второй одновременный запуск невозможен по определению."""

    def __init__(self, log: Callable[[str], None] | None = None) -> None:
        self._log = log or (lambda *_: None)
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._handler: _CallbackHandler | None = None
        self._port: int | None = None
        self._secret = ""
        self._fake_tls = ""
        self._last_error = ""

    # -- состояние ---------------------------------------------------------

    def available(self) -> bool:
        return available()

    def is_running(self) -> bool:
        thread = self._thread
        return bool(thread and thread.is_alive())

    def port(self) -> int | None:
        return self._port

    def secret(self) -> str:
        return self._secret

    def last_error(self) -> str:
        return self._last_error

    def link(self) -> str:
        if not self._port:
            return ""
        return deeplink(self._port, LOCAL_HOST, self._secret, self._fake_tls)

    def status(self) -> dict:
        return {
            "available": self.available(),
            "running": self.is_running(),
            "host": LOCAL_HOST,
            "port": self._port,
            "secret": self._secret,
            "link": self.link(),
            "fake_tls": self._fake_tls,
            "version": core_version(),
            "error": self._last_error,
        }

    # -- запуск / остановка ------------------------------------------------

    def start(self, *, port: int = DEFAULT_PORT, secret: str, dc_ips: Mapping | None = None,
              cfproxy: bool = True, h2: bool = True, cf_domains: Iterable[str] = (),
              fake_tls_domain: str = "", pool_size: int = 4,
              buffer_kb: int = 256) -> int:
        """Поднимает прокси. Возвращает фактический порт (может отличаться от
        запрошенного, если тот занят). Бросает TgProxyError, если поднять не вышло."""
        with self._lock:
            if self.is_running():
                return int(self._port or port)
            try:
                core_config, core_module = _core()
            except Exception as exc:  # noqa: BLE001
                raise TgProxyError(f"Ядро Telegram недоступно: {exc}") from exc

            actual_port = pick_port(int(port))
            cfg = core_config.proxy_config
            cfg.host = LOCAL_HOST
            cfg.port = actual_port
            cfg.secret = (secret or "").strip()
            cfg.dc_redirects = normalize_dc_ips(dc_ips)
            cfg.pool_size = max(0, int(pool_size))
            cfg.fallback_cfproxy = bool(cfproxy)
            cfg.cfproxy_h2_media = bool(h2)
            cfg.cfproxy_user_domains = [str(d).strip().lower()
                                        for d in (cf_domains or []) if str(d).strip()]
            cfg.disable_secure = False
            cfg.fake_tls_domain = (fake_tls_domain or "").strip()
            cfg.buffer_size = max(4, int(buffer_kb)) * 1024

            self._port = actual_port
            self._secret = cfg.secret
            self._fake_tls = cfg.fake_tls_domain
            self._last_error = ""
            self._attach_handler()

            self._thread = threading.Thread(target=self._worker, args=(core_module,),
                                            name="tgws-proxy", daemon=True)
            self._thread.start()

        if not self._wait_listening(actual_port):
            error = self._last_error or "прокси не открыл порт"
            self.stop()
            raise TgProxyError(error)
        self._log(f"tgws: прокси Telegram слушает {LOCAL_HOST}:{actual_port} "
                  f"(ядро {core_version()})")
        return actual_port

    def stop(self) -> None:
        with self._lock:
            thread, loop, stop_event = self._thread, self._loop, self._stop_event
            self._thread = self._loop = self._stop_event = None
        if loop is not None and stop_event is not None:
            try:
                loop.call_soon_threadsafe(stop_event.set)
            except Exception:  # noqa: BLE001  — цикл уже закрыт
                pass
        if thread is not None:
            thread.join(timeout=_STOP_TIMEOUT)
            if thread.is_alive():
                self._log("tgws: ядро не остановилось вовремя — закрываю принудительно")
                self._last_error = "ядро не остановилось вовремя"
        self._detach_handler()
        self._port = None

    # -- внутреннее --------------------------------------------------------

    def _worker(self, core_module) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        stop_event = asyncio.Event()
        self._loop, self._stop_event = loop, stop_event
        try:
            loop.run_until_complete(core_module._run(stop_event))  # noqa: SLF001
        except BaseException as exc:  # noqa: BLE001
            self._last_error = f"{type(exc).__name__}: {exc}"
            self._log(f"tgws: ядро упало: {self._last_error}")
        finally:
            try:
                loop.close()
            except Exception:  # noqa: BLE001
                pass

    def _wait_listening(self, port: int) -> bool:
        """Ждём, пока ядро откроет порт. Заодно ловим падение ядра на старте."""
        deadline = __import__("time").monotonic() + _START_TIMEOUT
        while __import__("time").monotonic() < deadline:
            thread = self._thread
            if thread is None or not thread.is_alive():
                return False
            try:
                with socket.create_connection((LOCAL_HOST, port), timeout=0.5):
                    return True
            except OSError:
                __import__("time").sleep(0.25)
        self._last_error = "прокси не начал слушать порт за отведённое время"
        return False

    def _attach_handler(self) -> None:
        if self._handler is not None:
            return
        handler = _CallbackHandler(self._log)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger = logging.getLogger(_CORE_LOGGER)
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        self._handler = handler

    def _detach_handler(self) -> None:
        if self._handler is None:
            return
        try:
            logging.getLogger(_CORE_LOGGER).removeHandler(self._handler)
        except Exception:  # noqa: BLE001
            pass
        self._handler = None


# ---------------------------------------------------------------------------
# Проверка «по ступеням» — чтобы сбой было видно, а не угадывать
# ---------------------------------------------------------------------------

def _check_direct(dc_ip: str, host: str = "kws2.web.telegram.org",
                  timeout: float = 12.0) -> tuple[bool, str]:
    """Прямой канал: TCP -> TLS с проверкой сертификата -> WebSocket /apiws.
    Ровно тот путь, которым ходит ядро."""
    try:
        import ssl
        import websockets

        ctx = ssl.create_default_context()
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.load_default_certs()
        try:
            ctx.set_alpn_protocols(["http/1.1"])
        except NotImplementedError:
            pass

        sock = socket.create_connection((dc_ip, 443), timeout=timeout)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError as exc:
        return False, f"нет связи с {dc_ip}: {exc}"
    except Exception as exc:  # noqa: BLE001
        return False, f"проверка недоступна: {exc}"

    async def _go() -> tuple[bool, str]:
        try:
            async with websockets.connect(f"wss://{host}/apiws", sock=sock, ssl=ctx,
                                          server_hostname=host, max_size=None,
                                          proxy=None, subprotocols=["binary"]) as ws:
                return True, f"канал открыт ({ws.state.name})"
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"

    try:
        return asyncio.run(_go())
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def _check_cf() -> tuple[bool, str]:
    """Резерв Cloudflare: берём пул доменов, который ядро уже загрузило, и смотрим,
    разрешается ли хоть один. Домены upstream закодированы сдвигом букв — пул
    расшифровывается самим ядром, поэтому спрашиваем его, а не список из репозитория."""
    try:
        from .tgws.balancer import balancer
    except Exception as exc:  # noqa: BLE001
        return False, f"ядро недоступно: {exc}"
    domains = list(getattr(balancer, "domains", []) or [])
    if not domains:
        return False, "пул доменов пуст (ядро ещё не загрузило список)"
    for domain in domains[:8]:
        try:
            socket.getaddrinfo(domain, 443, socket.AF_INET)
            return True, f"резерв отвечает: {domain} ({len(domains)} доменов в пуле)"
        except OSError:
            continue
    return False, f"ни один из {len(domains)} доменов резерва не разрешается"


def diagnose(*, running: bool = False, port: int | None = None,
             dc_ips: Mapping | None = None) -> dict:
    """Пошаговая проверка обхода Telegram. Возвращает строки-ступени и вердикт."""
    ips = normalize_dc_ips(dc_ips)
    rows: list[dict] = []

    rows.append({"step": "Ядро обхода",
                 "ok": available(),
                 "detail": f"tg-ws-proxy {core_version()}" if available()
                           else "файлы ядра не найдены"})

    if running and port:
        ok = not port_is_free(int(port))
        rows.append({"step": "Локальный прокси", "ok": ok,
                     "detail": f"127.0.0.1:{port}" + ("" if ok else " — порт не слушается")})
    else:
        rows.append({"step": "Локальный прокси", "ok": False,
                     "detail": "выключен — включи соединение Telegram"})

    direct_ip = ips.get(2) or next(iter(ips.values()), "")
    if direct_ip:
        ok, detail = _check_direct(direct_ip)
        rows.append({"step": "Прямой канал до Telegram", "ok": ok,
                     "detail": f"{direct_ip}: {detail}"})

    ok, detail = _check_cf()
    rows.append({"step": "Резерв через Cloudflare", "ok": ok, "detail": detail})

    bad = [r["step"] for r in rows if not r["ok"]]
    if not bad:
        verdict = "Соединение Telegram работает"
        hint = "Если в самом Telegram связи нет — переподключи прокси в нём."
    elif "Локальный прокси" in bad:
        verdict = "Прокси выключен"
        hint = "Включи «Соединение Telegram» и повтори проверку."
    elif "Прямой канал до Telegram" in bad:
        verdict = "Прямой адрес Telegram недоступен"
        hint = ("Провайдер блокирует адрес. Включи резерв через Cloudflare или "
                "задай свой домен в настройках.")
    else:
        verdict = "Работает частично"
        hint = "Прямой канал жив — резерв не нужен. Если медиа не грузятся, включи HTTP/2."
    return {"ok": not bad, "rows": rows, "verdict": verdict, "hint": hint}
