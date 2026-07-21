"""
FreeConnect — графическое приложение (pywebview).

Связывает фронтенд ui/index.html с движком: включение/выключение обхода,
автоподбор стратегии с живым прогрессом, мониторинг голосового и авто-восстановление.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

try:
    import webview
except ImportError:   # pywebview нужен только в рантайме GUI (create_window/start);
    webview = None    # для юнит-тестов/CI без GUI-зависимости модуль импортируется и так

from . import __version__, config, paths, tester, tgproxy, vpn
from .autosearch import StrategyScore, search
from .engine import Engine, EngineError, is_admin
from .monitor import VoiceMonitor
from .watchdog import ServiceWatchdog
from .doh import DoHManager
from .singbox import SingBox, SingBoxError
from .tgproxy import TgProxy, TgProxyError
from .strategies import Strategy, load_strategies


def _bundle_base() -> Path:
    """Корень ресурсов: временная папка PyInstaller (_MEIPASS) или корень репо."""
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))


def _ui_dir() -> Path:
    """Папка с фронтендом (рядом с исходником или в бандле PyInstaller)."""
    return _bundle_base() / "ui"


def _provision_runtime() -> None:
    """Первый запуск: разворачивает встроенный рантайм (winws/WinDivert/lists/
    strategies.json) в ASCII-путь C:\\FreeConnect\\runtime, если его там ещё нет.

    Нужно для собранного .exe у друзей: у них нет заранее подготовленного рантайма,
    а держать его надо в пути без кириллицы (иначе winws/WinDivert ломаются).
    """
    src = _bundle_base() / "runtime"
    if paths.runtime_ready():
        # Рантайм уже развёрнут (обновление поверх): полное копирование пропускаем,
        # но ДОКИДЫВАЕМ новые файлы бандла (напр. sing-box.exe для VPN), иначе фича
        # не появится у тех, кто ставит апдейт поверх готового C:\FreeConnect\runtime.
        _provision_missing(src)
        return
    if not src.is_dir():
        _log(f"provision: встроенный рантайм не найден ({src})")
        return
    import shutil
    paths.ensure_dirs()
    paths.RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        dst = paths.RUNTIME_DIR / item.name
        try:
            if item.is_dir():
                shutil.copytree(item, dst, dirs_exist_ok=True)
            elif not dst.exists():
                shutil.copy2(item, dst)
        except Exception as e:  # noqa: BLE001
            _log(f"provision copy failed {item.name}: {e}")
    _log(f"runtime provisioned -> {paths.RUNTIME_DIR} (ready={paths.runtime_ready()})")


def _provision_missing(src: Path) -> None:
    """Докидывает в уже развёрнутый рантайм только ОТСУТСТВУЮЩИЕ файлы из бандла
    (напр. новый sing-box.exe при обновлении поверх). Существующие не трогаем —
    иначе затёрли бы санитайзенные от BOM списки и вернули бы старые бинарники."""
    if not src.is_dir():
        return
    import shutil
    for root, _dirs, files in os.walk(src):
        rel = Path(root).relative_to(src)
        dst_dir = paths.RUNTIME_DIR / rel
        for name in files:
            dst = dst_dir / name
            if dst.exists():
                continue
            try:
                dst_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(Path(root) / name, dst)
                _log(f"provision: докинут {rel / name}")
            except Exception as e:  # noqa: BLE001
                _log(f"provision add failed {name}: {e}")


def _sanitize_lists() -> None:
    """Снимает UTF-8 BOM с файлов списков: winws читает BOM как битую первую
    строку («bad ip or subnet»), из-за чего, напр., ipset-all.txt грузится пустым."""
    try:
        lists_dir = paths.LISTS_DIR
        if not lists_dir.is_dir():
            return
        BOM = b"\xef\xbb\xbf"
        for f in lists_dir.glob("*.txt"):
            try:
                data = f.read_bytes()
                if data.startswith(BOM):
                    f.write_bytes(data[len(BOM):])
                    _log(f"BOM снят: {f.name}")
            except Exception:
                pass
    except Exception:
        pass


def _log(msg: str) -> None:
    """Живой лог этапов запуска — чтобы видеть, где зависло/упало."""
    try:
        paths.LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(paths.LOG_DIR / "debug.log", "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except Exception:
        pass


def _is_offerable(item: dict) -> bool:
    """Показывать стратегию пользователю? Да, если она строго рабочая ИЛИ реально
    открывает Discord по сайтам. Второе — чтобы не прятать кандидата вроде ALT9
    только из-за отрицательного (косвенного) UDP-замера голоса."""
    return bool(item.get("working") or item.get("discord_sites_ok"))


def _extract_exe_from_zip(zip_bytes: bytes, exe_name: str, dest) -> None:
    """Достаёт установщик из codeload-архива зеркала (zip оборачивает файлы в подпапку)
    в dest. Ищет по имени файла (суффиксу). Используется, когда GitHub недоступен и
    обновление качается с SourceCraft (там анонимно доступны только codeload-архивы)."""
    import io
    import shutil as _sh
    import zipfile
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        member = next((m for m in z.namelist()
                       if m == exe_name or m.endswith("/" + exe_name)), None)
        if not member:
            raise RuntimeError(f"в архиве зеркала нет {exe_name}")
        with z.open(member) as src, open(dest, "wb") as out:
            _sh.copyfileobj(src, out)


def _sc_all_sites_ok(sc) -> bool:
    """Все целевые сервисы стратегии открыты хотя бы по сайтам (TCP/TLS)."""
    svcs = getattr(sc, "services", [])
    return bool(svcs) and all(s.sites_ok for s in svcs)


def _is_discord_capable(w: dict) -> bool:
    """Стратегия способна тянуть Discord-голос (для ручного переключения). Считаем
    способной, если голос подтверждён (discord_ok), ИЛИ Discord открыт по сайтам
    (discord_sites_ok — гайд-кандидаты), ИЛИ это встроенная стратегия с рабочим
    Discord (discord>=2 из автоподбора, напр. ALT9). Так у кнопки есть реальный
    выбор, а не только подтверждённая одна."""
    return bool(w.get("discord_ok") or w.get("discord_sites_ok")
                or (w.get("discord") or 0) >= 2)


def _pick_switch_candidate(working: list[dict], current: str | None) -> str | None:
    """Следующая Discord-способная стратегия ПОСЛЕ текущей (по кругу), кроме самой
    текущей — для ручной кнопки «Сменить стратегию». Циклический обход, чтобы повторные
    нажатия перебирали все варианты, а не возвращали один и тот же."""
    caps = [w.get("name") for w in working
            if w.get("name") and _is_discord_capable(w)]
    if not caps:
        return None
    if current in caps:
        i = caps.index(current)
        for j in range(1, len(caps) + 1):
            cand = caps[(i + j) % len(caps)]
            if cand != current:
                return cand
        return None
    return caps[0]


def _score_to_item(sc: StrategyScore) -> dict:
    """StrategyScore -> словарь для JS (name как идентификатор)."""
    def svc(name: str) -> int:
        for s in sc.services:
            if s.service == name:
                return sum(1 for x in s.sites if x.ok)
        return 0

    def _svc(name: str):
        for s in sc.services:
            if s.service == name:
                return s
        return None

    disc = _svc("discord")
    yt = _svc("youtube")
    lat = sc.avg_latency_ms
    is_custom = (str(sc.strategy.id).startswith("custom_")
                 or sc.strategy.name.startswith("FreeConnect"))
    return {
        "id": sc.strategy.name,
        "name": sc.strategy.name,
        "discord": svc("discord"),
        "youtube": svc("youtube"),
        # *_ok — сервис прошёл ПОЛНОСТЬЮ (для Discord — вместе с живым голосом/медиа).
        # discord_sites_ok — Discord открыт хотя бы по TCP/TLS (сайты), даже если наш
        # косвенный UDP-замер голоса дал минус. Нужно, чтобы recovery переключался
        # только на Discord-способную стратегию, а автоподбор не прятал такую как ALT9.
        "discord_ok": bool(disc.ok) if disc else False,
        "youtube_ok": bool(yt.ok) if yt else False,
        "discord_sites_ok": bool(disc.sites_ok) if disc else False,
        "latency": round(lat) if lat >= 0 else None,
        "working": sc.working,
        "custom": is_custom,
    }


class Api:
    """Методы, доступные из JS через window.pywebview.api.*"""

    def __init__(self) -> None:
        self.engine = Engine()
        self.cfg = config.load()
        self.enabled = False
        self.strategy_name: str | None = self.cfg.get("strategy")
        self.working: list[dict] = self.cfg.get("working", [])
        self._cancel = threading.Event()
        # on_update намеренно None: не пушим пинг каждые 3с через evaluate_js
        # (UI и так опрашивает status()); это снижает нагрузку и риск подвисаний.
        self.monitor = VoiceMonitor(
            on_update=None, on_spike=self._on_voice_spike
        )
        # Watchdog доступности Discord/YouTube по TCP/TLS — дополняет голосовой монитор
        # (UDP). Деградация сервиса -> тот же авто-ремонт, что и всплеск голоса.
        self.watchdog = ServiceWatchdog(
            services_provider=lambda: ["discord", "youtube"],
            on_degraded=self._on_watchdog_degraded,
        )
        # DNS-over-HTTPS (опция, по умолчанию выкл): шифрует DNS, обходит DNS-подмену.
        self.doh = DoHManager(log=_log)
        # VPN-для-Discord: движок sing-box + разобранный список серверов из подписки.
        # Best-effort и полностью отдельно от winws (см. singbox.py). Серверы восстанавливаем
        # из кэша конфига при старте, чтобы список был доступен без повторного импорта.
        self.singbox = SingBox(log=_log)
        self._vpn_servers: list = []
        try:
            cached = self.cfg.get("vpn_config", "")
            if cached:
                self._vpn_servers = vpn.parse_servers(cached)
        except Exception as e:  # noqa: BLE001
            _log(f"vpn: не разобрал кэш подписки: {e}")
        # Обход Telegram: локальный SOCKS5→WebSocket прокси (см. tgproxy.py). Полностью
        # отдельно от winws/sing-box, прав админа не требует. Best-effort.
        self.tgproxy = TgProxy(log=_log,
                               endpoints=self.cfg.get("tg_endpoints") or None)
        self._tg_discovering = False   # идёт ли фоновый поиск живого адреса
        # Пассивный детектор голоса (эксперим., по умолчанию выкл): наблюдает реальный
        # медиапоток Discord через WinDivert SNIFF и ловит односторонний/мёртвый голос,
        # который STUN-монитор и watchdog не видят. Best-effort: сбой не рушит обход.
        self._voicewatch = None
        self._searching = False
        self._recovering = False
        # Гайд-подтверждение голоса (ground truth от живого Discord): бэкенд включает
        # кандидата и ждёт вердикт человека («Голос подключился!» / «Дальше»).
        self._vc_event = threading.Event()
        self._vc_verdict: bool | None = None
        self._last_recovery = 0.0
        self.recovery_cooldown = 30.0  # сек между авто-восстановлениями
        self._recover_count = 0        # сколько попыток восстановления подряд
        self.restart_before_switch = 2 # столько раз перезапускаем текущую, потом переключаемся
        self.recover_reset_after = 180.0  # если N сек всё ок — сбросить счётчик попыток
        self._recover_tried: set[str] = set()  # опробованные в текущей серии (не биться меж двух)
        self._recovery_paused_until = 0.0      # пауза после «перепробовал все» (антидребезг)
        self.recovery_exhaust_pause = 120.0    # сколько молчать, перебрав весь пул
        # окно / трей / фон
        self._win = None
        self.tray = None
        self._really_quit = False
        self.autostart_mode = False
        self.tray_enabled = True
        self.frameless = False
        self._diag_progress = None
        self._diag_thread = None
        # состояние проверки обновлений приложения (заполняет фоновый поток)
        self._update_info: dict = {"available": False, "version": "", "url": "", "notes": "",
                                   "source": "", "exe": ""}
        # Очередь событий бэкенд->UI. КРИТИЧНО: evaluate_js нельзя звать из фоновых
        # потоков (WebView2: "can only be accessed from the UI thread" → флуд COM-
        # исключений и зависание окна). Поэтому фон только КЛАДЁТ событие в очередь,
        # а JS сам её опрашивает (poll_events) в UI-потоке и вызывает window.onX.
        self._events: list[dict] = []
        self._events_lock = threading.Lock()

    # ---- вспомогательное ----
    def _window(self):
        return webview.windows[0] if webview.windows else None

    def _push(self, func: str, *args) -> None:
        """Положить событие для UI в очередь (JS заберёт через poll_events)."""
        with self._events_lock:
            self._events.append({"fn": func, "args": list(args)})
            # Защита от неограниченного роста, если UI долго не опрашивает.
            if len(self._events) > 300:
                self._events = self._events[-300:]

    def poll_events(self) -> list:
        """UI забирает накопленные события (вызывается из JS в UI-потоке)."""
        with self._events_lock:
            ev = self._events
            self._events = []
        return ev

    def dbg_dump(self, text: str) -> None:
        """Слить JS-таймлайн (метки этапов + разрывы кадров) в debug.log."""
        try:
            _log("---- JS TIMELINE ----")
            for line in str(text).splitlines():
                _log("  " + line)
            _log("---- END JS TIMELINE ----")
        except Exception:
            pass

    def js_log(self, msg: str) -> None:
        """Живая метка со стороны JS — сразу пишем в debug.log (не ждём конца)."""
        _log("JS: " + str(msg))

    def _find_strategy(self, name: str) -> Strategy | None:
        for s in load_strategies():
            if s.name == name:
                return s
        return None

    @staticmethod
    def _strategy_rank(w: dict) -> int:
        """Ранг стратегии для авто-выбора: оба сервиса=3 > только Discord=2 >
        только YouTube=1 > прочее=0. Для своих стратегий Discord-«живость» берём
        из голос-валидированной метки в имени (All/Discord) + защита от битых
        данных (сайты>0); для встроенных — по числу открытых сайтов."""
        name = w.get("name", "") or ""
        d = w.get("discord", 0) or 0
        y = w.get("youtube", 0) or 0
        custom = w.get("custom", False)
        if custom:
            disc = (name.endswith(" All") or name.endswith(" Discord")) and d >= 1
            yt = name.endswith(" All") or name.endswith(" YouTube") or y >= 2
        else:
            disc = d >= 3
            yt = y >= 3
        return (2 if disc else 0) + (1 if yt else 0)

    def _best_strategy_name(self) -> str | None:
        """Лучшая стратегия для авто-подключения — работает и для Discord, и для
        YouTube. Если текущая сохранённая уже в топе — не трогаем её."""
        cands = [w for w in self.working if w.get("name")]
        if not cands:
            return self.strategy_name
        best_rank = max(self._strategy_rank(w) for w in cands)
        top = [w["name"] for w in cands if self._strategy_rank(w) == best_rank]
        if self.strategy_name in top:
            return self.strategy_name
        return top[0]

    def _state(self) -> dict:
        # В список подмешиваем свои стратегии (FreeConnect #N) — они всегда доступны.
        working = list(self.working)
        try:
            from .custom import load_custom
            names = {w["name"] for w in working}
            for s in load_custom():
                if s.name not in names:
                    working.append({"id": s.name, "name": s.name, "discord": 3,
                                    "youtube": 3, "latency": None, "working": True, "custom": True})
        except Exception:
            pass
        return {
            "enabled": self.enabled,
            "strategy": self.strategy_name,
            "working": working,
            "admin": is_admin(),
            "onboarded": bool(self.cfg.get("onboarded", False)),
            "update": dict(self._update_info),
            "version": __version__,
            "vpn_on": self.singbox.is_running(),  # для бейджа «Discord через VPN» на главном
            "tg_on": self.tgproxy.is_running(),   # для бейджа «Telegram» на главном
        }

    def _save(self) -> None:
        self.cfg["strategy"] = self.strategy_name
        self.cfg["working"] = self.working
        config.save(self.cfg)

    # ---- API для JS ----
    def get_state(self) -> dict:
        return self._state()

    def set_onboarded(self, val: bool = True) -> None:
        """Запоминаем, что обучение пройдено (в config.json — переживает перезапуск)."""
        self.cfg["onboarded"] = bool(val)
        config.save(self.cfg)

    def collect_logs(self) -> dict:
        """Собирает логи+конфиг в один zip на рабочем столе и открывает проводник.
        Друг просто пересылает этот файл разработчику — сервер не нужен."""
        import zipfile
        import platform
        import datetime
        try:
            profile = os.environ.get("USERPROFILE", str(paths.APP_HOME))
            desktop = Path(profile) / "Desktop"
            out_dir = desktop if desktop.is_dir() else Path(profile)
            ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            zip_path = out_dir / f"FreeConnect-логи-{ts}.zip"

            try:
                from . import webview2, strategy_update
                wv2 = webview2.is_installed()
            except Exception:
                wv2 = "?"
            try:
                from .custom import load_custom
                n_custom = len(load_custom())
            except Exception:
                n_custom = "?"
            info = [
                f"FreeConnect diagnostics {ts}",
                f"OS: {platform.platform()}",
                f"admin: {is_admin()}   frozen(exe): {getattr(sys, 'frozen', False)}",
                f"WebView2 installed: {wv2}",
                f"strategy: {self.strategy_name}   enabled: {self.enabled}",
                f"settings: {self.get_settings()}",
                f"custom strategies: {n_custom}",
                f"strategies_updated_at: {self.cfg.get('strategies_updated_at')}",
            ]
            files = [
                paths.LOG_DIR / "debug.log",
                paths.LOG_DIR / "debug.prev.log",  # прошлая сессия (до перезапуска/апдейта)
                paths.LOG_DIR / "winws.log",
                paths.CONFIG_PATH,
            ]
            try:
                from .custom import CUSTOM_PATH
                files.append(CUSTOM_PATH)
            except Exception:
                pass
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
                z.writestr("info.txt", "\n".join(info))
                for f in files:
                    try:
                        if f.is_file():
                            z.write(f, f.name)
                    except Exception:
                        pass
            _log(f"collect_logs -> {zip_path}")
            try:
                subprocess.Popen(["explorer", "/select,", str(zip_path)])
            except Exception:
                pass
            return {"ok": True, "path": str(zip_path)}
        except Exception as e:  # noqa: BLE001
            _log(f"collect_logs failed: {e}")
            return {"ok": False, "error": str(e)}

    def is_frameless(self) -> bool:
        return self.frameless

    # ---- стартовая автоматизация (сплэш) ----
    # Диагностика идёт в ФОНОВОМ потоке (сеть не блокирует UI), а JS быстро
    # ОПРАШИВАЕТ прогресс. Ни evaluate_js, ни сети в UI-потоке — окно не замирает.
    def start_diagnostics(self) -> None:
        if self._diag_thread is not None and self._diag_thread.is_alive():
            return
        from . import diagnostics
        self._diag_progress = {"i": 0, "total": len(diagnostics.STEPS), "pct": 0,
                               "label": "", "status": None, "detail": "", "done": False}
        self._diag_thread = threading.Thread(target=self._run_diagnostics, daemon=True)
        self._diag_thread.start()

    def _run_diagnostics(self) -> None:
        from . import diagnostics
        from .diagnostics import Check
        _log("diagnostics begin")
        steps = diagnostics.STEPS
        total = len(steps)
        for i, (label, fn) in enumerate(steps):
            self._diag_progress = {"i": i, "total": total, "pct": round(i / total * 100),
                                   "label": label, "status": None, "detail": "", "done": False}
            _log(f"diag step {i}: {label}")
            try:
                c = fn()
            except Exception as e:  # noqa: BLE001
                c = Check(label, "warn", str(e)[:120])
            self._diag_progress = {"i": i, "total": total, "pct": round((i + 1) / total * 100),
                                   "label": c.name, "status": c.status, "detail": c.detail,
                                   "done": False}
        try:
            self.cfg = config.load()
            auto = self.cfg.get("auto_enable", True)
            # По умолчанию поднимаем ЛУЧШУЮ стратегию (работает и для Discord, и
            # для YouTube), а не последнюю сохранённую — иначе автозапуск мог
            # поднять ютуб-онли (как ловил юзер).
            best = self._best_strategy_name()
            if best and best != self.strategy_name:
                _log(f"autoconnect: выбрана лучшая стратегия {best} (была {self.strategy_name})")
                self.strategy_name = best
                self._save()
            _log(f"autoconnect check: autostart_mode={self.autostart_mode} "
                 f"auto_enable={auto} strategy={self.strategy_name}")
            if self.autostart_mode and auto and self.strategy_name:
                # На свежей загрузке WinDivert-драйвер/сеть могут быть не готовы —
                # winws падает кодом 1. Ретраим с паузой, пока не поднимется.
                for attempt in range(1, 6):
                    _log(f"autoconnect attempt {attempt}…")
                    self.enable()
                    if self.enabled:
                        break
                    _log(f"autoconnect attempt {attempt} failed")
                    time.sleep(4)
                _log(f"autoconnect: enabled={self.enabled}")
            # VPN-для-Discord: если пользователь оставил туннель включённым (A1 —
            # держим, пока сам не выключит), поднимаем его при старте. Best-effort:
            # нет бинарника/серверов/прав — просто пропускаем, обход winws не страдает.
            if self.cfg.get("vpn_enabled") and self.singbox.available() and self._vpn_servers:
                _log("autoconnect: восстанавливаю VPN-туннель для Discord")
                res = self.vpn_set_enabled(True)
                if not res.get("ok"):
                    _log(f"autoconnect vpn: {res.get('error', 'не поднялся')}")
            # Обход Telegram: поднимаем прокси при старте, если пользователь его не выключал.
            if self.cfg.get("tg_enabled") and self.tgproxy.available():
                _log("autoconnect: восстанавливаю прокси Telegram")
                res = self.tg_set_enabled(True)
                if not res.get("ok"):
                    _log(f"autoconnect tg: {res.get('error', 'не поднялся')}")
        except Exception as e:  # noqa: BLE001
            _log(f"diag finish err: {e}")
        p = dict(self._diag_progress)
        p["pct"], p["done"] = 100, True
        self._diag_progress = p
        _log("diagnostics done")
        # Обновление стратегий из upstream — в ФОНЕ (20 сетевых запросов не должны
        # задерживать автоподключение). Свежий набор применится со следующего старта.
        threading.Thread(target=self._bg_update_strategies, daemon=True).start()
        # Проверка новой версии самого приложения (GitHub Releases) — тоже в фоне.
        threading.Thread(target=self._bg_check_update, daemon=True).start()
        # Свежие адреса веб-входа Telegram из нашего канала: если зашитый адрес
        # заблокируют, обход починится сам, без действий пользователя.
        threading.Thread(target=self._bg_update_tg_endpoints, daemon=True).start()

    def _bg_update_tg_endpoints(self) -> None:
        try:
            from . import endpoint_update
            merged, err = endpoint_update.maybe_update()
            if merged:
                self.cfg["tg_endpoints"] = merged
                self.tgproxy.set_endpoints(merged)
                _log(f"tg endpoints (bg): обновлены -> {merged}")
            else:
                _log(f"tg endpoints (bg): без изменений ({err})")
        except Exception as e:  # noqa: BLE001
            _log(f"tg endpoints (bg) failed: {e}")

    def _bg_update_strategies(self) -> None:
        try:
            from . import strategy_update
            n, err = strategy_update.maybe_update()
            _log(f"strategy update (bg): обновлено={n} err={err!r}")
        except Exception as e:  # noqa: BLE001
            _log(f"strategy update (bg) failed: {e}")

    def _bg_check_update(self) -> None:
        try:
            from . import app_update
            info = app_update.check()
            if info.get("error"):
                _log(f"app update check [{info.get('source', '?')}]: {info['error']}")
            else:
                self._update_info = {k: info.get(k, "")
                                     for k in ("available", "version", "url", "notes", "source", "exe")}
                _log(f"app update [{info.get('source', '?')}]: "
                     f"available={info['available']} version={info['version']!r}")
        except Exception as e:  # noqa: BLE001
            _log(f"app update check failed: {e}")

    def check_app_update(self) -> dict:
        """Ручная проверка обновления (кнопка в настройках). Возвращает состояние."""
        self._bg_check_update()
        return dict(self._update_info)

    def open_url(self, url: str) -> None:
        """Открывает ссылку в браузере по умолчанию (страница/установщик релиза)."""
        try:
            if url and url.startswith(("http://", "https://")):
                import webbrowser
                webbrowser.open(url)
        except Exception as e:  # noqa: BLE001
            _log(f"open_url failed: {e}")

    def install_update(self) -> dict:
        """Тихое автообновление: качает установщик и ставит поверх БЕЗ запроса UAC
        (мы уже под админом) и БЕЗ диалогов. Приложение само закроется, освободив
        файлы, а установщик после установки перезапустит его ([Run] без skipifsilent).
        Возвращает {ok} сразу; работа идёт в фоне, статус — через события."""
        info = self._update_info or {}
        url = info.get("url", "")
        source = info.get("source", "github")
        exe_name = info.get("exe", "")
        if source == "mirror":
            # с зеркала (SourceCraft) установщик приходит в codeload-архиве (.zip)
            if not (url.startswith(("http://", "https://")) and "/zipball/" in url):
                return {"ok": False, "error": "нет ссылки на архив зеркала"}
        elif not (url.startswith(("http://", "https://")) and url.lower().endswith(".exe")):
            return {"ok": False, "error": "нет прямой ссылки на установщик"}
        threading.Thread(target=self._do_install_update, args=(url, source, exe_name),
                         daemon=True).start()
        return {"ok": True}

    def _do_install_update(self, url: str, source: str = "github", exe_name: str = "") -> None:
        import shutil as _sh
        import urllib.request
        try:
            if not getattr(sys, "frozen", False):
                raise RuntimeError("автообновление только в собранном приложении")
            exe = sys.executable  # путь установленного FreeConnect.exe (после установки — новый)
            # Файлы кладём в ASCII-путь C:\FreeConnect (не в %TEMP% с кириллицей —
            # cmd/пути с кириллицей ненадёжны).
            paths.ensure_dirs()
            base = paths.APP_HOME
            setup = base / "FreeConnect-Setup-update.exe"
            bat = base / "FreeConnect-update.bat"

            req = urllib.request.Request(url, headers={"User-Agent": "FreeConnect"})
            if source == "mirror":
                # codeload-архив с установщиком внутри — качаем и распаковываем в setup.
                with urllib.request.urlopen(req, timeout=120) as r:
                    data = r.read()
                _extract_exe_from_zip(data, exe_name or "FreeConnect-Setup.exe", setup)
                _log(f"update (mirror) extracted -> {setup} ({setup.stat().st_size} b)")
            else:
                with urllib.request.urlopen(req, timeout=60) as r, open(setup, "wb") as f:
                    _sh.copyfileobj(r, f)
                _log(f"update downloaded -> {setup} ({setup.stat().st_size} b)")

            # Трамплин: ждёт завершения тихого установщика, затем стартует приложение
            # как ОБЫЧНЫЙ запуск (start = ShellExecute) — чистое окружение, onefile
            # распаковывается корректно. Раньше перезапуск делал сам установщик через
            # [Run] runascurrentuser и onefile падал «python3xx.dll не найден».
            bat.write_text(
                "@echo off\r\n"
                ":wait\r\n"
                'tasklist /fi "imagename eq FreeConnect-Setup-update.exe" 2>nul | '
                'find /i "FreeConnect-Setup-update.exe" >nul\r\n'
                "if not errorlevel 1 (\r\n"
                "  ping -n 2 127.0.0.1 >nul\r\n"
                "  goto wait\r\n"
                ")\r\n"
                "ping -n 3 127.0.0.1 >nul\r\n"
                f'start "" "{exe}"\r\n'
                'del "%~f0"\r\n',
                encoding="ascii")

            DETACHED = 0x00000008 | 0x00000200   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
            subprocess.Popen(
                [str(setup), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NOCANCEL", "/NORESTART"],
                creationflags=DETACHED, close_fds=True)
            subprocess.Popen(
                ["cmd", "/c", str(bat)],
                creationflags=DETACHED | 0x08000000,  # + CREATE_NO_WINDOW
                close_fds=True)
            _log("update: установщик+трамплин запущены -> закрываюсь для замены файлов")
            time.sleep(0.8)
            self._tray_quit()   # освобождаем FreeConnect.exe; трамплин перезапустит после установки
        except Exception as e:  # noqa: BLE001
            _log(f"install_update failed: {e}")
            self._push("onUpdateError", str(e))

    def get_startup_progress(self) -> dict:
        return self._diag_progress or {"i": 0, "total": 6, "pct": 0,
                                       "label": "", "status": None, "detail": "", "done": False}

    # ---- управление окном (кастомный заголовок) + трей ----
    def minimize_window(self) -> None:
        try:
            (self._win or self._window()).minimize()
        except Exception:
            pass

    def close_window(self) -> None:
        # Крестик прячет в трей; если трея нет — обычное закрытие.
        if self.tray:
            self.hide_to_tray()
        else:
            self._tray_quit()

    def on_gui_ready(self) -> None:
        """Вызывается pywebview В ОТДЕЛЬНОМ ПОТОКЕ уже после старта GUI-цикла."""
        _log("GUI READY (webview loaded, gui-loop started)")
        if not self.tray_enabled:
            return
        _log("gui ready -> start_tray")
        self.start_tray()
        _log("tray started")

    def hide_to_tray(self) -> None:
        try:
            (self._win or self._window()).hide()
        except Exception:
            pass

    def start_tray(self) -> None:
        try:
            from .tray import Tray
            self.tray = Tray(on_show=self._tray_show, on_toggle=self._tray_toggle,
                             on_quit=self._tray_quit)
            self.tray.start()
        except Exception as e:  # noqa: BLE001
            _log(f"tray failed: {e}")
            self.tray = None

    def _tray_show(self) -> None:
        try:
            w = self._win or self._window()
            w.show()
            w.restore()
        except Exception:
            pass

    def _tray_toggle(self) -> None:
        if self.enabled:
            self.disable()
        else:
            self.enable()
        self._push("onExternalState", self._state())

    def _tray_quit(self) -> None:
        self._really_quit = True
        try:
            self._stop_monitors()
            self._stop_doh()   # синхронно: вернуть DNS адаптера ДО выхода
            self.engine.stop()
            self.singbox.stop()  # снять TUN и маршруты VPN, не осиротить туннель
            self.tgproxy.stop()  # закрыть локальный SOCKS5 Telegram
        except Exception:
            pass
        if self.tray:
            self.tray.stop()
        try:
            (self._win or self._window()).destroy()
        except Exception:
            pass

    def on_closing(self):
        """Обработчик закрытия окна: прячем в трей вместо выхода."""
        try:
            if self._really_quit or not self.tray:
                return True   # без трея — обычное закрытие
            self.hide_to_tray()
            return False
        except Exception as e:  # noqa: BLE001
            _log(f"on_closing error: {e}")
            return True

    def get_settings(self) -> dict:
        # autostart читаем из РЕАЛЬНОГО состояния задачи Планировщика, а не из cfg —
        # чтобы галка не врала (в dev-режиме без .exe задача не создаётся вовсе).
        try:
            from . import autostart
            autostart_on = autostart.is_enabled()
        except Exception:
            autostart_on = self.cfg.get("autostart", False)
        return {
            "autostart": autostart_on,
            "autostart_available": bool(getattr(sys, "frozen", False)),
            "monitor": self.cfg.get("monitor", True),
            "auto_enable": self.cfg.get("auto_enable", True),
            "game_filter": self.cfg.get("game_filter", False),
            "doh": self.cfg.get("doh", False),
            "voice_confirm": self.cfg.get("voice_confirm", False),
            "voice_watch": self.cfg.get("voice_watch", False),
        }

    def set_setting(self, key: str, value) -> dict:
        if key not in ("autostart", "monitor", "auto_enable", "game_filter", "doh",
                       "voice_confirm", "voice_watch"):
            return self.get_settings()
        val = bool(value)
        if key == "autostart":
            ok = False
            try:
                from . import autostart
                ok = autostart.set_enabled(val)
            except Exception as e:  # noqa: BLE001
                _log(f"autostart set error: {e}")
            # В cfg пишем ФАКТ: если задачу не удалось создать (нет .exe) — off.
            self.cfg["autostart"] = val and ok
            if val and not ok:
                _log("autostart: задача НЕ создана (нужен собранный .exe)")
        else:
            self.cfg[key] = val
        config.save(self.cfg)
        # GameFilter меняет аргументы winws — если обход включён, перезапускаем,
        # чтобы игровые порты применились сразу.
        if key == "game_filter" and self.enabled:
            _log(f"game_filter -> {val}: перезапуск обхода")
            self.enable()
        # DoH включаем/выключаем сразу (смена DNS адаптера идёт в фоне, чтобы не тормозить UI).
        if key == "doh":
            if val and self.enabled:
                self._start_doh_async()
            elif not val:
                threading.Thread(target=self._stop_doh, daemon=True).start()
        # Детектор голоса включаем/выключаем на лету, если обход уже работает.
        if key == "voice_watch" and self.enabled:
            if val:
                self._start_voicewatch()
            elif self._voicewatch is not None:
                try:
                    self._voicewatch.stop()
                except Exception:  # noqa: BLE001
                    pass
                self._voicewatch = None
        return self.get_settings()

    # ---- VPN-для-Discord ----
    _VPN_PROTO = {"hysteria2": "Hysteria2", "vless-reality": "VLESS-Reality",
                  "trojan-reality": "Trojan-Reality"}

    def _vpn_country_ru(self, country: str) -> str:
        return vpn.COUNTRIES.get(country, ("", "Сервер"))[1]

    def _vpn_server_rows(self) -> list:
        """Список стран для UI: по одной строке на страну (лучший протокол —
        серверы уже отсортированы Hysteria2→VLESS→Trojan, поэтому берём первый)."""
        rows, seen = [], set()
        for s in self._vpn_servers:
            if s.country in seen:
                continue
            seen.add(s.country)
            rows.append({"id": s.country, "country": s.country,
                         "name": self._vpn_country_ru(s.country),
                         "sub": self._VPN_PROTO.get(s.kind, s.kind)})
        return rows

    def _vpn_title(self, server) -> str:
        return f"{self._vpn_country_ru(server.country)} · {self._VPN_PROTO.get(server.kind, server.kind)}"

    def vpn_get_state(self) -> dict:
        return {
            "available": self.singbox.available(),   # забандлен ли бинарник sing-box
            "imported": bool(self._vpn_servers),
            "servers": self._vpn_server_rows(),
            "selected": self.cfg.get("vpn_country", "") or "auto",
            "enabled": self.singbox.is_running(),
            "sub_url": self.cfg.get("vpn_sub_url", ""),
        }

    # Разные sub-сервисы отдают конфиг ТОЛЬКО «знакомым» клиентам и режут чужой
    # User-Agent (напр. skippnet: наш UA → HTTP 445, а под Streisand отдаёт полный
    # xray-JSON). Перебираем распространённые UA и берём ответ, из которого удалось
    # вытащить больше всего серверов. Streisand первым — под него отдают Happ-JSON.
    _SUB_UAGENTS = ["Streisand", "v2rayNG/1.9.5", "Happ/1.11.0", "clash-verge/1.5",
                    "sing-box", "FreeConnect"]

    def _fetch_subscription_servers(self, url: str):
        """Скачивает подписку, перебирая User-Agent; возвращает (text, servers) с
        максимумом распознанных серверов. Бросает последнюю сетевую ошибку, если
        ни один UA не ответил."""
        import urllib.request
        best_text, best = "", []
        last_err = None
        for ua in self._SUB_UAGENTS:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": ua})
                with urllib.request.urlopen(req, timeout=12) as r:  # noqa: S310 (ввод юзера)
                    text = r.read().decode("utf-8", "replace")
            except Exception as e:  # noqa: BLE001
                last_err = e
                continue
            try:
                servers = vpn.parse_servers(text)
            except Exception:  # noqa: BLE001
                servers = []
            if len(servers) > len(best):
                best_text, best = text, servers
            if len(best) >= 2:   # достаточно — не гоняем остальные UA
                break
        if not best_text and last_err is not None:
            raise last_err
        return best_text, best

    def vpn_import(self, url: str = "", json_text: str = "") -> dict:
        """Импорт подписки: скачать по ссылке ИЛИ разобрать вставленный JSON.
        Наполняет список серверов и кэширует его в конфиг."""
        url = (url or "").strip()
        json_text = (json_text or "").strip()
        if json_text:
            text = json_text
            try:
                servers = vpn.parse_servers(text)
            except Exception as e:  # noqa: BLE001
                _log(f"vpn: не разобрал вставленный конфиг: {e}")
                return {**self.vpn_get_state(), "ok": False, "error": "Не разобрал конфиг — формат не распознан"}
        elif url:
            try:
                text, servers = self._fetch_subscription_servers(url)
            except Exception as e:  # noqa: BLE001
                _log(f"vpn: скачать подписку не вышло: {e}")
                return {**self.vpn_get_state(), "ok": False,
                        "error": "Не удалось скачать подписку — проверь ссылку и интернет"}
        else:
            return {**self.vpn_get_state(), "ok": False, "error": "Вставь ссылку-подписку или конфиг JSON"}

        if not servers:
            return {**self.vpn_get_state(), "ok": False,
                    "error": "В подписке нет подходящих зарубежных серверов (Hysteria2/VLESS/Trojan)"}

        self._vpn_servers = servers
        self.cfg["vpn_config"] = text
        if url:
            self.cfg["vpn_sub_url"] = url
        config.save(self.cfg)
        st = self.vpn_get_state()
        st["ok"] = True
        st["message"] = f"Импортировано стран: {len(st['servers'])}"
        return st

    def vpn_select(self, country: str = "") -> dict:
        """Выбор страны-выхода ('auto' = лучший по приоритету). Если туннель уже
        поднят — перезапускаем на новый сервер."""
        country = (country or "").strip()
        if country == "auto":
            country = ""
        self.cfg["vpn_country"] = country
        config.save(self.cfg)
        if self.singbox.is_running():
            return self.vpn_set_enabled(True)
        return {**self.vpn_get_state(), "ok": True}

    def vpn_set_enabled(self, on) -> dict:
        """Включить/выключить туннель. On: строим конфиг под выбранный сервер и
        запускаем sing-box; Off: гасим. Обход winws при этом не трогаем."""
        on = bool(on)
        if not on:
            try:
                self.singbox.stop()
            except Exception as e:  # noqa: BLE001
                _log(f"vpn stop: {e}")
            self.cfg["vpn_enabled"] = False
            config.save(self.cfg)
            self._push("onVpnState", False)
            return {**self.vpn_get_state(), "ok": True, "message": "VPN для Discord выключен"}

        if not self._vpn_servers:
            return {**self.vpn_get_state(), "ok": False, "error": "Сначала импортируй подписку"}
        country = self.cfg.get("vpn_country", "") or None
        server = vpn.best_server(self._vpn_servers, country=country)
        if not server:
            return {**self.vpn_get_state(), "ok": False, "error": "Нет сервера для выбранной страны"}
        try:
            self.singbox.start(vpn.build_singbox_config(server))
        except SingBoxError as e:
            return {**self.vpn_get_state(), "ok": False, "error": str(e)}
        except Exception as e:  # noqa: BLE001
            _log(f"vpn start: {e}")
            return {**self.vpn_get_state(), "ok": False, "error": f"Не удалось поднять туннель: {e}"}
        self.cfg["vpn_enabled"] = True
        config.save(self.cfg)
        self._push("onVpnState", True)
        st = self.vpn_get_state()
        st["ok"] = True
        st["message"] = f"Discord идёт через VPN: {self._vpn_title(server)}"
        return st

    # ---- Обход Telegram (локальный SOCKS5→WebSocket, см. tgproxy.py) ----
    def tg_get_state(self) -> dict:
        port = int(self.cfg.get("tg_port", tgproxy.DEFAULT_PORT))
        return {
            "available": self.tgproxy.available(),   # есть ли зависимости (всегда для нашей сборки)
            "enabled": self.tgproxy.is_running(),
            "port": port,
            "host": tgproxy.LOCAL_HOST,
            "deeplink": tgproxy.deeplink(port),
            # Telegram настроен ходить через наш прокси, поэтому без запущенного
            # FreeConnect он не работает вовсе. Если автозапуск выключен — UI
            # предупредит, иначе после перезагрузки человек решит, что сломался
            # Telegram, а не поймёт, что надо запустить программу.
            "autostart": bool(self.cfg.get("autostart", False)),
        }

    def tg_set_enabled(self, on) -> dict:
        """Включить/выключить прокси Telegram. On: поднять SOCKS5; Off: погасить.
        winws/VPN при этом не трогаем — они независимы."""
        on = bool(on)
        if not on:
            try:
                self.tgproxy.stop()
            except Exception as e:  # noqa: BLE001
                _log(f"tg stop: {e}")
            self.cfg["tg_enabled"] = False
            config.save(self.cfg)
            self._push("onTgState", False)
            return {**self.tg_get_state(), "ok": True, "message": "Соединение Telegram выключено"}

        port = int(self.cfg.get("tg_port", tgproxy.DEFAULT_PORT))
        try:
            self.tgproxy.start(port=port)
        except TgProxyError as e:
            return {**self.tg_get_state(), "ok": False, "error": str(e)}
        except Exception as e:  # noqa: BLE001
            _log(f"tg start: {e}")
            return {**self.tg_get_state(), "ok": False, "error": f"Не удалось запустить прокси: {e}"}
        self.cfg["tg_enabled"] = True
        config.save(self.cfg)
        self._push("onTgState", True)
        return {**self.tg_get_state(), "ok": True,
                "message": f"Прокси Telegram слушает {tgproxy.LOCAL_HOST}:{port}"}

    def tg_autoconfigure(self) -> dict:
        """Открывает tg://socks-ссылку: Telegram спросит, добавить ли наш прокси
        (это и есть «Настроить автоматически»). Прокси при этом должен быть включён."""
        if not self.tgproxy.is_running():
            res = self.tg_set_enabled(True)
            if not res.get("ok"):
                return res
        port = int(self.cfg.get("tg_port", tgproxy.DEFAULT_PORT))
        link = tgproxy.deeplink(port)
        try:
            os.startfile(link)  # noqa: S606  зарегистрированный обработчик tg:// = Telegram Desktop
        except Exception as e:  # noqa: BLE001
            _log(f"tg deeplink: {e}")
            return {**self.tg_get_state(), "ok": False,
                    "error": "Не удалось открыть Telegram — задай прокси вручную: "
                             f"Настройки → Продвинутые → Тип соединения → SOCKS5, "
                             f"{tgproxy.LOCAL_HOST}:{port}"}
        return {**self.tg_get_state(), "ok": True,
                "message": "Открыл Telegram — подтверди добавление прокси в приложении"}

    def tg_diagnose(self) -> dict:
        """Проверка обхода Telegram «по ступеням» — чтобы человек (и я по скриншоту)
        видел, что именно сломалось: блокировка адресов, TLS или веб-сокет."""
        try:
            res = tgproxy.diagnose(endpoints=self.cfg.get("tg_endpoints") or None)
        except Exception as e:  # noqa: BLE001
            _log(f"tg diagnose: {e}")
            return {"ok": False, "verdict": f"Не удалось выполнить проверку: {e}",
                    "rows": []}

        rows = res.get("rows", [])
        alive = [r["ip"] for r in rows if r.get("ok")]
        if alive:
            verdict = f"Соединение Telegram работает — живой адрес: {', '.join(alive)}"
            hint = ""
        elif not rows:
            verdict = "Не с чем работать: нет ни одного адреса для проверки"
            hint = "Похоже, не отвечает DNS. Проверь интернет."
        elif all("нет ответа" in r.get("tcp", "") for r in rows):
            verdict = "Провайдер блокирует все известные адреса Telegram"
            hint = ("Нужен новый рабочий адрес — сообщи мне, обновлю список "
                    "(или дождись автопоиска в следующей версии).")
        else:
            verdict = "До сервера доходим, но соединение не устанавливается"
            hint = "Пришли эту таблицу — разберу по ступеням."
        _log(f"tg diagnose: ok={res.get('ok')} живых={len(alive)} из {len(rows)}")
        return {**res, "verdict": verdict, "hint": hint}

    def tg_discover(self) -> dict:
        """Ищет новый живой адрес веб-входа Telegram, если встроенный заблокировали.
        Работает в фоне: перебор долгий, UI ждать не должен."""
        if getattr(self, "_tg_discovering", False):
            return {"ok": False, "error": "Поиск уже идёт"}
        if not self.tgproxy.available():
            return {"ok": False, "error": "Соединение Telegram недоступно"}
        self._tg_discovering = True
        threading.Thread(target=self._tg_discover_worker, daemon=True,
                         name="tg-discover").start()
        return {"ok": True, "started": True}

    def _tg_discover_worker(self) -> None:
        dc = tgproxy.DEFAULT_DC
        try:
            _log("tg discover: старт поиска живого адреса")
            found = tgproxy.discover(
                dc=dc, progress=lambda p: self._push("onTgDiscover", p))
            if not found:
                _log("tg discover: живых адресов не найдено")
                self._push("onTgDiscoverDone",
                           {"ok": False, "found": [],
                            "message": "Живых адресов не нашлось — попробуй позже "
                                       "или включи VPN и сообщи мне"})
                return
            # ДЦ2 и ДЦ4 обслуживает один узел, поэтому адрес годится обоим.
            eps = dict(self.cfg.get("tg_endpoints") or {})
            for key in (str(dc), "4") if dc == 2 else (str(dc),):
                eps[key] = found
            self.cfg["tg_endpoints"] = eps
            config.save(self.cfg)
            self.tgproxy.set_endpoints(eps)
            _log(f"tg discover: найдено {found}, сохранено в конфиг")
            if self.tgproxy.is_running():   # переподнять, чтобы новые адреса пошли в дело
                self.tg_set_enabled(False)
                self.tg_set_enabled(True)
            self._push("onTgDiscoverDone",
                       {"ok": True, "found": found,
                        "message": f"Найден рабочий адрес: {', '.join(found)}"})
        except Exception as e:  # noqa: BLE001
            _log(f"tg discover err: {e}")
            self._push("onTgDiscoverDone", {"ok": False, "found": [],
                                            "message": f"Поиск не удался: {e}"})
        finally:
            self._tg_discovering = False

    def enable(self) -> dict:
        if not self.strategy_name:
            return self._state()
        strat = self._find_strategy(self.strategy_name)
        if not strat:
            return self._state()
        try:
            self.engine.start(strat)
            self.enabled = True
            self._start_monitors()
            self._start_doh_async()
        except EngineError as e:
            self.enabled = False
            self._push("onError", str(e))
        if self.tray:
            self.tray.set_active(self.enabled)
        return self._state()

    def disable(self) -> dict:
        self._stop_monitors()
        threading.Thread(target=self._stop_doh, daemon=True).start()  # откат DNS в фоне
        self.engine.stop()
        self.enabled = False
        if self.tray:
            self.tray.set_active(False)
        return self._state()

    def pick_strategy(self, name: str) -> dict:
        self.strategy_name = name
        self._reset_recovery_state(grace=20.0)  # ручной выбор — фора от авто-ремонта
        self._save()
        return self.enable()

    def delete_strategy(self, name: str) -> dict:
        """Убирает стратегию из списка; если она своя (FreeConnect #N) — стирает с диска."""
        try:
            from .custom import load_custom, delete_custom
            for s in load_custom():
                if s.name == name:
                    delete_custom(s.id)
                    break
        except Exception:
            pass
        self.working = [w for w in self.working if w.get("name") != name]
        if self.strategy_name == name:
            self.strategy_name = None
        self._save()
        return self._state()

    def clear_strategies(self) -> dict:
        """Очищает весь список рабочих стратегий и удаляет все свои с диска."""
        try:
            from .custom import load_custom, delete_custom
            for s in load_custom():
                delete_custom(s.id)
        except Exception:
            pass
        self.working = []
        self.strategy_name = None
        self._save()
        return self._state()

    def status(self) -> dict:
        """Лёгкая проверка доступности для индикаторов."""
        if not self.enabled:
            return {"discord": None, "youtube": None, "voice": None}
        # Лёгкая проверка достижимости (без 4-сек теста «заморозки») — чтобы опрос
        # статуса не блокировал вызовы к бэкенду и окно не подвисало.
        with ThreadPoolExecutor(max_workers=2) as ex:
            fd = ex.submit(tester.check_site, "gateway.discord.gg", "/", timeout=2.5, probe_freeze=False)
            fy = ex.submit(tester.check_site, "www.youtube.com", "/", timeout=2.5, probe_freeze=False)
            d, y = fd.result(), fy.result()
        rtt = self.monitor.last_rtt
        return {
            "discord": {"ok": d.ok, "latency": round(d.latency_ms) if d.latency_ms >= 0 else None},
            "youtube": {"ok": y.ok, "latency": round(y.latency_ms) if y.latency_ms >= 0 else None},
            "voice": {"rtt": round(rtt) if rtt is not None else None},
        }

    def start_search(self) -> None:
        if self._searching:
            return
        self._searching = True
        self._cancel.clear()
        threading.Thread(target=self._run_search, daemon=True).start()

    def cancel_search(self) -> None:
        self._cancel.set()
        self._vc_event.set()   # разбудить ожидание гайд-подтверждения голоса

    def voice_confirm_result(self, connected: bool) -> dict:
        """Вердикт человека на текущего кандидата в гайд-проверке голоса:
        True — «в Discord голос подключился», False — «дальше, не подключился»."""
        self._vc_verdict = bool(connected)
        self._vc_event.set()
        return {"ok": True}

    def start_deep_search(self) -> None:
        if self._searching:
            return
        self._searching = True
        self._cancel.clear()
        threading.Thread(target=self._run_deep, daemon=True).start()

    def refresh_strategies(self) -> None:
        """Пере-тест уже сохранённых стратегий (без генерации): поднимаем каждую заново,
        меряем доступность Discord/YouTube и задержку, затем пересортировываем список по
        актуальному состоянию. Нужно, т.к. пинг/доступность у провайдера меняются во
        времени, а список хранит замеры на момент подбора."""
        if self._searching:
            return
        self._searching = True
        self._cancel.clear()
        threading.Thread(target=self._run_refresh, daemon=True).start()

    def _run_refresh(self) -> None:
        from .autosearch import evaluate_strategy
        from .tester import DEFAULT_TARGETS
        self._stop_monitors()
        svcs = list(DEFAULT_TARGETS.keys())
        items = list(self.working)
        total = len(items)
        prev_active = self.strategy_name
        was_enabled = self.enabled
        updated: list[dict] = []
        try:
            _log(f"=== REFRESH: пере-тест {total} стратегий ===")
            for i, w in enumerate(items):
                if self._cancel.is_set():
                    break
                name = w.get("name")
                self._push("onSearchProgress", i, total, name or "?")
                strat = self._find_strategy(name) if name else None
                if not strat:
                    updated.append(w)          # нет объекта стратегии — оставляем как есть
                    continue
                try:
                    sc = evaluate_strategy(self.engine, strat, svcs)
                except Exception as e:  # noqa: BLE001
                    _log(f"refresh: «{name}» ошибка: {e}")
                    updated.append(w)
                    continue
                it = _score_to_item(sc)
                it["name"] = it["id"] = name
                it["custom"] = w.get("custom", False)
                # Подтверждённость голоса человеком сайт-замер не знает — не теряем её.
                if w.get("voice_confirmed"):
                    it["voice_confirmed"] = True
                    it["discord_ok"] = True
                self._push("onSearchResult", i, total, name,
                           {"discord": it["discord"], "youtube": it["youtube"]})
                updated.append(it)
            # После «Стоп» непроверенные стратегии сохраняем как были (не теряем список).
            done = {u.get("name") for u in updated}
            updated += [w for w in items if w.get("name") not in done]
        finally:
            updated.sort(key=lambda w: (-self._strategy_rank(w),
                                        w.get("latency") if w.get("latency") is not None else 10**9))
            self.working = updated
            # Пере-тест гонял движок по всем стратегиям — возвращаем ранее активную.
            restored = False
            if was_enabled and prev_active:
                strat = self._find_strategy(prev_active)
                if strat:
                    try:
                        self.engine.start(strat)
                        self.strategy_name = prev_active
                        self._start_monitors()
                        restored = True
                    except EngineError as e:  # noqa: BLE001
                        _log(f"refresh: не вернуть активную «{prev_active}»: {e}")
            if was_enabled and not restored:
                self.enabled = False       # честно: движок стоит — не показываем «включено»
            self._save()
            self._searching = False
            self._push("onSearchDone", self._state()["working"])

    def _run_deep(self) -> None:
        from . import deepsearch
        self._stop_monitors()
        # База для мутаций: текущая стратегия -> лучшая рабочая -> ALT -> первая встроенная.
        base_name = self.strategy_name or (self.working[0]["name"] if self.working else None)
        base = (self._find_strategy(base_name) if base_name else None) or self._find_strategy("ALT")
        if not base:
            base = load_strategies()[0]

        # Точная проверка голоса: вместо STUN-прокси гайдим человека подтвердить голос
        # в его живом Discord (единственный честный ground truth без залогина).
        if self.cfg.get("voice_confirm", False):
            self._run_deep_guided(base)
            return

        ctx = {"i": 0, "total": 0}

        def on_progress(i, total, cand):
            ctx["i"], ctx["total"] = i, total
            self._push("onSearchProgress", i, total, cand.name)

        def on_found(saved, sc):
            item = _score_to_item(sc)
            item["custom"] = True
            self._push("onSearchFound", item)

        def on_result(sc):
            # Детальный лог для реальной диагностики генерации (почему Discord не прошёл).
            try:
                parts = []
                for s in sc.services:
                    sites = ",".join(f"{x.host.split('.')[0]}={x.status}" for x in s.sites)
                    v = "" if s.voice_ok is None else (
                        f" voice={'ok' if s.voice_ok else 'DEAD'}/{s.voice_conf}[{s.voice_detail}]")
                    parts.append(f"{s.service}[{sites}{v}] ok={s.ok}")
                _log(f"CAND {sc.strategy.name}: working={sc.working} label={sc.result_label()!r} | "
                     + " | ".join(parts))
            except Exception:
                pass
            self._push("onSearchResult", ctx["i"], ctx["total"], sc.strategy.name,
                       {"discord": 0, "youtube": 0})

        results = []
        try:
            _log(f"=== DEEP SEARCH start (base={base.name}) ===")
            results = deepsearch.deep_search(
                base, engine=self.engine, budget=120, stop_on_all=True, min_all=5,
                on_progress=on_progress, on_result=on_result, on_found=on_found,
                cancel=self._cancel,
            )
            _log(f"=== DEEP SEARCH done: кандидатов={len(results)}, "
                 f"рабочих={sum(1 for r in results if r.working)} ===")
        except Exception as e:  # noqa: BLE001
            self._push("onError", f"Ошибка глубокого поиска: {e}")

        cancelled = self._cancel.is_set()
        working = [_score_to_item(r) for r in results if r.working]
        names = {w["name"] for w in working}
        # Найденное всегда добавляем в список (даже при «Стоп» — не сбрасываем перебор).
        self.working = working + [w for w in self.working if w["name"] not in names]
        if working and not cancelled:
            # Полный проход завершён: working отсортирован по баллу (services_ok, затем
            # качество голоса) — working[0] это лучшая All с быстрым/стабильным голосом.
            self.strategy_name = working[0]["name"]
            self._save()
            self.enable()
        else:
            # «Стоп»: сохраняем найденное в списке, но НЕ трогаем активную стратегию —
            # иначе ранняя остановка после одной YouTube-only перекидывала на неё
            # (тот баг: «нашёл одну ютуб-стратегию и переключил на неё»). Пусть человек
            # выберет сам; текущий рабочий обход не рушим.
            self._save()
        self._searching = False
        self._push("onSearchDone", self._state()["working"])

    def _run_deep_guided(self, base) -> None:
        """Гайд-подтверждение голоса: собираем шорт-лист кандидатов (Discord открыт по
        сайтам), затем по очереди включаем каждого и ждём, пока человек подтвердит в
        живом Discord, что голос подключился. Первое «Да» — честно подтверждённая
        стратегия, фиксируем её. winws под подтверждённым кандидатом НЕ перезапускаем,
        чтобы не оборвать уже поднятый голос."""
        from . import custom, deepsearch

        def on_progress(i, total, cand):
            self._push("onSearchProgress", i, total, cand.name)

        # Бутстрап: СРАЗУ поднимаем обход на известной рабочей стратегии, чтобы Discord
        # мог открыться и достучаться до канала ещё ДО того, как мы попросим человека
        # зайти в голосовой (иначе — тот самый тупик: «зайди в канал», а канал не
        # подключается, потому что обход ещё не включён). Стратегию НЕ сохраняем и НЕ
        # фиксируем — это лишь временный обход на время подбора; collect_site_candidates
        # ниже переиспользует этот же движок и продолжит перебор.
        boot = (self._find_strategy(self.strategy_name) if self.strategy_name else None) or base
        try:
            self.engine.start(boot)
            self._push("onGuidedBootstrap", {"name": getattr(boot, "name", "")})
            _log(f"guided: бутстрап-обход «{getattr(boot, 'name', '?')}» — Discord может открыться")
        except EngineError as e:
            _log(f"guided: бутстрап не поднялся: {e}")

        shortlist: list = []
        try:
            _log("=== GUIDED VOICE: сбор шорт-листа (Discord по сайтам) ===")
            shortlist = deepsearch.collect_site_candidates(
                base, engine=self.engine, want=5, budget=120,
                on_progress=on_progress, cancel=self._cancel)
        except Exception as e:  # noqa: BLE001
            self._push("onError", f"Ошибка поиска: {e}")

        if self._cancel.is_set():
            self._searching = False
            self._push("onVoiceConfirmDone", {"confirmed": False, "cancelled": True})
            return
        if not shortlist:
            _log("guided: кандидатов с открытым Discord не нашлось")
            self._searching = False
            self._push("onVoiceConfirmDone", {"confirmed": False, "empty": True})
            return

        # Сохраняем ВЕСЬ шорт-лист как пул своих стратегий сразу. Даже неподтверждённые
        # нужны для ручной кнопки «Сменить стратегию»: раньше сохранялась только
        # подтверждённая — и переключаться при мёртвом голосе было НЕ НА ЧТО.
        # discord_ok=False (голос ещё не подтверждён человеком), но discord_sites_ok=True.
        pool: list[dict] = []
        saved_strats: list = []
        for sc in shortlist:
            label = "All" if _sc_all_sites_ok(sc) else "Discord"
            saved = custom.add_custom(sc.strategy.args,
                                      base_name=(base.name if base else "auto"),
                                      label=label)
            saved_strats.append(saved)
            item = _score_to_item(sc)
            item["name"] = item["id"] = saved.name
            item["custom"] = True
            item["discord_ok"] = False
            item["discord_sites_ok"] = True
            pool.append(item)
        names = {p["name"] for p in pool}
        self.working = pool + [w for w in self.working if w["name"] not in names]
        self._save()

        confirmed = None
        total = len(shortlist)
        for idx, sc in enumerate(shortlist):
            if self._cancel.is_set():
                break
            saved = saved_strats[idx]
            strat = self._find_strategy(saved.name) or sc.strategy
            try:
                self.engine.start(strat)  # поднимаем winws; застрявший Discord доберётся
            except EngineError as e:
                _log(f"guided: не поднять кандидата «{saved.name}»: {e}")
                continue
            self._vc_verdict = None
            self._vc_event.clear()
            self._push("onVoiceConfirmProbe", {"index": idx + 1, "total": total,
                                               "name": saved.name})
            lat = pool[idx].get("latency")
            _log(f"guided: кандидат {idx + 1}/{total} «{saved.name}» ({lat}мс) — жду вердикт")
            got = self._vc_event.wait(timeout=90)   # даём человеку время увидеть и нажать
            if self._cancel.is_set():
                break
            if got and self._vc_verdict:
                # Человек подтвердил живой голос — метим этого кандидата и делаем активным.
                for p in self.working:
                    if p["name"] == saved.name:
                        p["discord_ok"] = True        # человек подтвердил живой голос
                        p["voice_confirmed"] = True
                        confirmed = p
                        break
                self.strategy_name = saved.name
                self.working = ([confirmed] +
                                [w for w in self.working if w["name"] != saved.name])
                self._save()
                _log(f"guided: ПОДТВЕРЖДЁН голос на «{saved.name}»")
                break

        if confirmed:
            # winws уже крутит подтверждённого кандидата — не перезапускаем, только
            # доводим состояние до «включено» и поднимаем мониторы/DoH.
            self.enabled = True
            self._start_monitors()
            self._start_doh_async()
            self._push("onExternalState", self._state())
        else:
            # Никого не подтвердили — гасим движок, но пул сохранён: пользователь может
            # запустить любую из стратегий вручную (кнопкой «Сменить стратегию»).
            try:
                self.engine.stop()
            except Exception:  # noqa: BLE001
                pass

        self._searching = False
        self._push("onVoiceConfirmDone", {
            "confirmed": bool(confirmed),
            "name": confirmed["name"] if confirmed else "",
            "working": self._state()["working"],
        })

    # ---- внутреннее: поиск ----
    def _run_search(self) -> None:
        # На время поиска гасим мониторы/обход.
        self._stop_monitors()
        counter = {"i": 0}

        def on_progress(i, total, strat):
            counter["i"] = i
            self._push("onSearchProgress", i, total, strat.name)

        def on_result(sc: StrategyScore):
            item = _score_to_item(sc)
            i = counter["i"]
            total = len(load_strategies())
            self._push("onSearchResult", i, total, sc.strategy.name,
                       {"discord": item["discord"], "youtube": item["youtube"]})
            # Показываем стратегию, если она строго рабочая ИЛИ реально открывает
            # Discord по сайтам — не прячем от юзера кандидата вроде ALT9 только из-за
            # отрицательного (косвенного) UDP-замера голоса.
            if sc.working or item["discord_sites_ok"]:
                self._push("onSearchFound", item)

        try:
            results = search(
                engine=self.engine,
                on_progress=on_progress,
                on_result=on_result,
                cancel=self._cancel,
            )
        except Exception as e:  # noqa: BLE001
            self._push("onError", f"Ошибка поиска: {e}")
            results = []

        items = [_score_to_item(r) for r in results]
        # «Предлагаемые» = строго рабочие (с живым голосом) ЛИБО те, где Discord открыт
        # по сайтам. Второе — чтобы не прятать стратегию, которую юзер потом включает
        # руками (кейс ALT9): наш UDP-замер голоса косвенный и может ошибиться в минус.
        offerable = [it for it in items if _is_offerable(it)]
        self.working = offerable
        # Авто-включаем только строго проверенную лучшую (голос подтверждён); если такой
        # нет — не навязываем, показываем список, юзер выберет (напр. тот же ALT9).
        working = [it for it in offerable if it["working"]]
        if working:
            self.strategy_name = working[0]["name"]
            self._save()
            self.enable()
        self._searching = False
        self._push("onSearchDone", offerable)

    # ---- мониторы (голос по UDP + доступность сервисов по TCP/TLS) ----
    def _start_monitors(self) -> None:
        if not self.cfg.get("monitor", True):
            return
        # STUN-монитор голоса запускаем ТОЛЬКО когда точная проверка голоса выключена:
        # его сигнал (пинг Google STUN) ненадёжен и на части сетей STUN мёртв вовсе —
        # тогда он ложно «роняет» и переключает даже стратегию, где голос реально живой
        # (в т.ч. подтверждённую человеком). С voice_confirm доверяем человеку + watchdog.
        if not self.cfg.get("voice_confirm", False):
            self.monitor.start()
        self.watchdog.start()
        # Пассивный SNIFF-детектор голоса (эксперим.). Работает и вместе с voice_confirm:
        # там STUN-монитор выключен, а этот видит реальный медиапоток. Best-effort.
        if self.cfg.get("voice_watch", False):
            self._start_voicewatch()

    def _start_voicewatch(self) -> None:
        if self._voicewatch is not None:
            return
        try:
            from .voicewatch import VoiceWatch
            self._voicewatch = VoiceWatch(on_dead=self._on_voice_dead, log=_log)
            self._voicewatch.start()
        except Exception as e:  # noqa: BLE001
            _log(f"voicewatch: не поднять (ок): {e}")
            self._voicewatch = None

    def _stop_monitors(self) -> None:
        self.monitor.stop()
        self.watchdog.stop()
        if self._voicewatch is not None:
            try:
                self._voicewatch.stop()
            except Exception:  # noqa: BLE001
                pass
            self._voicewatch = None

    def _on_voice_dead(self, reason: str) -> None:
        # Детектор голоса увидел односторонний/мёртвый поток. Сообщаем UI (с подсказкой
        # про регион — часть таких смертей это RTC-сервер, а не стратегия) и запускаем
        # тот же авто-ремонт, что и STUN-всплеск/watchdog (общий cooldown/счётчик).
        self._push("onVoiceDead", {"reason": reason})
        self._trigger_recovery(f"voicewatch: {reason}")

    def _start_doh_async(self) -> None:
        """Поднять DoH в фоне (смена DNS через PowerShell занимает ~1с — не тормозим UI)."""
        if not self.cfg.get("doh", False):
            return
        def go() -> None:
            try:
                ok = self.doh.start()
                self._push("onDohState", bool(ok))
                if not ok:
                    _log("DoH не удалось включить (см. выше)")
            except Exception as e:  # noqa: BLE001
                _log(f"doh start error: {e}")
                self._push("onDohState", False)
        threading.Thread(target=go, daemon=True).start()

    def _stop_doh(self) -> None:
        try:
            self.doh.stop()
        except Exception as e:  # noqa: BLE001
            _log(f"doh stop error: {e}")

    def _on_voice_update(self, rtt) -> None:
        self._push("onVoiceUpdate", rtt)

    def _on_voice_spike(self) -> None:
        # Пинг голосового скакнул / потеря пакетов — уведомляем UI и восстанавливаем.
        self._push("onVoiceSpike")
        self._trigger_recovery("voice spike")

    def _on_watchdog_degraded(self, service: str, status: str) -> None:
        # Сервис (Discord/YouTube) устойчиво не открывается по TCP/TLS — тот же авто-ремонт.
        self._push("onServiceDegraded", {"service": service, "status": status})
        self._trigger_recovery(f"watchdog: {service} {status}")

    def _trigger_recovery(self, reason: str) -> None:
        """Единая точка авто-восстановления для голосового монитора и watchdog:
        общий cooldown и счётчик попыток, чтобы источники не били одновременно."""
        if not (self.enabled and self.cfg.get("monitor", True)):
            _log(f"{reason}, но восстановление off (enabled/monitor)")
            return
        now = time.monotonic()
        if now < self._recovery_paused_until:
            return   # только что перебрали весь пул — держим паузу, не дребезжим
        if self._recovering or (now - self._last_recovery) < self.recovery_cooldown:
            return
        # Если проблем давно не было — считаем путь здоровым, сбрасываем счётчик и
        # список опробованных (следующая серия начнётся с чистого перебора).
        if now - self._last_recovery > self.recover_reset_after:
            self._recover_count = 0
            self._recover_tried = set()
        self._recovering = True
        self._last_recovery = now
        self._recover_count += 1
        _log(f"{reason} -> recovery attempt #{self._recover_count}")
        threading.Thread(target=self._recover, daemon=True).start()

    def _recover(self) -> None:
        """Эскалация восстановления голоса:
        1-2 попытки — перезапуск текущей стратегии (аналог «перезайти в канал»);
        дальше — переключение на следующую рабочую стратегию (текущую душит ТСПУ)."""
        try:
            if self._recover_count <= self.restart_before_switch:
                strat = self._find_strategy(self.strategy_name) if self.strategy_name else None
                if strat:
                    _log(f"recovery: перезапуск текущей «{self.strategy_name}»")
                    self._push("onRecovering")
                    self.engine.start(strat)
            else:
                self._switch_to_next_working()
        except EngineError as e:
            _log(f"recovery error: {e}")
            self._push("onError", str(e))
        finally:
            self._recovering = False

    def _switch_to_next_working(self) -> None:
        """Переключиться на следующую Discord-способную стратегию — ПО КРУГУ и по ВСЕМУ
        пулу, включая встроенные (ALT9, FAKE TLS AUTO ALT3 и т.п.).

        Раньше брали «первую с discord_ok, кроме текущей» — а discord_ok=True только у
        подтверждённых человеком, поэтому восстановление вечно билось между двумя
        сгенерированными (#13↔#11) и НЕ добиралось до встроенной FAKE TLS AUTO ALT3,
        которая реально держала голос. Теперь идём циклически (_pick_switch_candidate,
        широкий _is_discord_capable) и запоминаем опробованные: перебрав весь пул без
        успеха, сообщаем юзеру (проблема, похоже, на стороне RTC-сервера — сменить регион)
        и держим паузу, чтобы не молотить бесконечно."""
        caps = [w.get("name") for w in self.working
                if w.get("name") and _is_discord_capable(w)]
        nxt = _pick_switch_candidate(self.working, self.strategy_name)
        if not nxt:
            strat = self._find_strategy(self.strategy_name) if self.strategy_name else None
            if strat:
                _log("recovery: нет альтернатив с живым Discord — перезапуск текущей")
                self._push("onRecovering")
                self.engine.start(strat)
            return
        strat = self._find_strategy(nxt)
        if not strat:
            return
        _log(f"recovery: ПЕРЕКЛЮЧЕНИЕ «{self.strategy_name}» -> «{nxt}» (Discord-способная)")
        self._push("onRecovering")
        self.engine.start(strat)
        if self.strategy_name:
            self._recover_tried.add(self.strategy_name)
        self._recover_tried.add(nxt)
        self.strategy_name = nxt
        # Перебрали весь пул Discord-способных и голос так и не поднялся — это уже не
        # стратегия, а путь/сервер. Сообщаем и берём паузу (антидребезг).
        if caps and self._recover_tried.issuperset(caps):
            _log("recovery: перебран весь пул, голос не держится — пауза + подсказка про регион")
            self._push("onRecoveryExhausted")
            self._recover_tried = set()
            self._recovery_paused_until = time.monotonic() + self.recovery_exhaust_pause
        self._save()
        self._push("onExternalState", self._state())

    def manual_voice_switch(self) -> dict:
        """Ручное переключение по кнопке «Голос лагает — сменить стратегию».

        Надёжный автодетект смерти именно Discord-голоса без залогина невозможен
        (нет IP RTC-сервера/SSRC; STUN до Google ≠ голос Discord). Поэтому источник
        правды — уши человека: один клик переключает на следующую Discord-способную
        стратегию (по кругу), не заставляя заново проходить весь подбор.

        Возвращает {'switched': bool, 'name': str, 'reason': str}.
        """
        # Берём полный список (свои + встроенные), как его видит UI.
        working = self._state()["working"]
        nxt = _pick_switch_candidate(working, self.strategy_name)
        if not nxt:
            _log("manual switch: нет других Discord-способных стратегий")
            return {"switched": False, "name": "", "reason": "no_candidates"}
        strat = self._find_strategy(nxt)
        if not strat:
            return {"switched": False, "name": "", "reason": "not_found"}
        _log(f"MANUAL SWITCH: «{self.strategy_name}» -> «{nxt}»")
        try:
            self.engine.start(strat)
        except EngineError as e:
            self._push("onError", str(e))
            return {"switched": False, "name": "", "reason": "engine"}
        self.strategy_name = nxt
        self.enabled = True
        self._reset_recovery_state(grace=20.0)  # ручной выбор — не сбивать его авто-ремонтом
        self._start_monitors()
        if self.tray:
            self.tray.set_active(True)
        self._save()
        self._push("onExternalState", self._state())
        return {"switched": True, "name": nxt, "reason": ""}

    def _reset_recovery_state(self, grace: float = 0.0) -> None:
        """Сброс авто-восстановления (при ручном выборе стратегии). grace — короткая
        фора, чтобы залётное «голос мёртв» от ПРЕДЫДУЩЕЙ стратегии сразу не сбило только
        что выбранную."""
        self._recover_tried = set()
        self._recover_count = 0
        self._recovery_paused_until = time.monotonic() + grace if grace else 0.0


_SINGLE_INSTANCE_HANDLE = None


def _acquire_single_instance() -> bool:
    """True — мы единственная копия; False — уже запущена другая (надо выйти).

    Ручка мьютекса держится в глобале, чтобы жила всё время работы процесса
    (иначе GC закроет её и защита пропадёт). Нужна и сама по себе (winws/WinDivert
    не терпят второй копии), и при автообновлении: установщик и трамплин из старой
    версии могут одновременно попытаться открыть приложение — второй запуск тихо выйдет.
    """
    global _SINGLE_INSTANCE_HANDLE
    try:
        import ctypes
        ERROR_ALREADY_EXISTS = 183
        h = ctypes.windll.kernel32.CreateMutexW(None, False, "Global\\FreeConnect_SingleInstance_v1")
        if not h:
            return True  # не смогли создать мьютекс — не блокируем запуск
        if ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            return False
        _SINGLE_INSTANCE_HANDLE = h
        return True
    except Exception:
        return True  # на всякий случай не мешаем запуску


def run(argv: list[str] | None = None) -> None:
    args = argv if argv is not None else sys.argv[1:]

    # Одна копия на систему. При автообновлении старая версия уже вышла (мьютекс
    # освобождён), поэтому новая копия спокойно стартует; лишний параллельный запуск
    # (установщик + трамплин) — тихо завершится, без второго окна.
    if not _acquire_single_instance():
        _log("FreeConnect уже запущен — выходим (одна копия)")
        return

    paths.ensure_dirs()
    # НЕ затираем прошлый лог: сохраняем его в debug.prev.log. Иначе перезапуск (в т.ч.
    # авто-восстановление/переоткрытие после апдейта) стирал улики предыдущей сессии —
    # именно из-за этого баги «после обновления» приходилось диагностировать вслепую.
    try:
        cur = paths.LOG_DIR / "debug.log"
        if cur.exists():
            prev = paths.LOG_DIR / "debug.prev.log"
            try:
                prev.unlink()
            except FileNotFoundError:
                pass
            cur.replace(prev)
    except Exception:
        pass
    _log(f"=== run start (v{__version__}, args={args}) ===")
    _provision_runtime()  # первый запуск .exe: развернуть рантайм в ASCII-путь
    _sanitize_lists()     # снять BOM со списков (иначе winws не грузит ipset)

    # Страховка: без WebView2 Runtime окно не создастся. Проверяем/ставим ДО окна.
    try:
        from . import webview2
        if not webview2.is_installed():
            _log("WebView2 отсутствует — пробую установить")
            if not webview2.ensure(_log):
                _log("WebView2 поставить не удалось — показываю сообщение и выходю")
                webview2.show_missing_message()
                return
            _log("WebView2 установлен")
    except Exception as e:  # noqa: BLE001
        _log(f"WebView2 check error: {e}")
    api = Api()
    # Флаги (по умолчанию — максимально стабильная конфигурация):
    #  трей (pystray) и своя рамка (frameless) ОТКЛЮЧЕНЫ — они вызывали зависания
    #  инициализации WebView2. Включаются явными флагами для отладки.
    # Баг зависания (рекурсия pywebview по нативному окну) исправлен — трей и своя
    # рамка снова включены по умолчанию. Отключить при отладке: --no-tray / --no-frameless.
    api.tray_enabled = "--no-tray" not in args
    api.frameless = "--no-frameless" not in args
    minimized = api.tray_enabled and (("--tray" in args) or ("--minimized" in args))
    api.autostart_mode = minimized
    index = _ui_dir() / "index.html"

    # Аварийный режим: отключить аппаратное ускорение WebView2 (флаг --safe).
    if "--safe" in args:
        os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = "--disable-gpu"
        _log("safe mode: --disable-gpu")

    _log(f"create_window (frameless={api.frameless})")
    window = webview.create_window(
        "FreeConnect",
        url=str(index),
        js_api=api,
        width=600,
        height=920,
        min_size=(480, 760),
        background_color="#070912",
        frameless=api.frameless,
        hidden=minimized,
    )
    api._win = window
    if api.tray_enabled:
        try:
            window.events.closing += api.on_closing
        except Exception as e:  # noqa: BLE001
            _log(f"closing subscribe failed: {e}")
    _log("webview.start")
    # Простой запуск (как в стабильной версии): без своей папки данных WebView2 —
    # постоянная папка вызывала зависание init (блокировка от прошлого запуска).
    # Колбэк готовности передаём ВСЕГДА — чтобы в лог попадала метка «GUI READY»
    # (диагностика: поднялся ли вообще WebView2-мост).
    webview.start(api.on_gui_ready)
    _log("=== webview.start returned (exit) ===")
    # Страховка на любой путь выхода (в т.ч. закрытие окна без трея): не осиротить
    # VPN-туннель — снять TUN и маршруты sing-box.
    try:
        api.singbox.stop()
        api.tgproxy.stop()
    except Exception:
        pass


if __name__ == "__main__":
    try:
        run()
    except Exception as e:  # noqa: BLE001
        import traceback
        _log("FATAL: " + repr(e) + "\n" + traceback.format_exc())
        raise
