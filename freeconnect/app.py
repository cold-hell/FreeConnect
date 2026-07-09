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

import webview

from . import __version__, config, paths, tester
from .autosearch import StrategyScore, search
from .engine import Engine, EngineError, is_admin
from .monitor import VoiceMonitor
from .watchdog import ServiceWatchdog
from .doh import DoHManager
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
    if paths.runtime_ready():
        return
    src = _bundle_base() / "runtime"
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


def _score_to_item(sc: StrategyScore) -> dict:
    """StrategyScore -> словарь для JS (name как идентификатор)."""
    def svc(name: str) -> int:
        for s in sc.services:
            if s.service == name:
                return sum(1 for x in s.sites if x.ok)
        return 0
    lat = sc.avg_latency_ms
    is_custom = (str(sc.strategy.id).startswith("custom_")
                 or sc.strategy.name.startswith("FreeConnect"))
    return {
        "id": sc.strategy.name,
        "name": sc.strategy.name,
        "discord": svc("discord"),
        "youtube": svc("youtube"),
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
        self._searching = False
        self._recovering = False
        self._last_recovery = 0.0
        self.recovery_cooldown = 30.0  # сек между авто-восстановлениями
        self._recover_count = 0        # сколько попыток восстановления подряд
        self.restart_before_switch = 2 # столько раз перезапускаем текущую, потом переключаемся
        self.recover_reset_after = 180.0  # если N сек всё ок — сбросить счётчик попыток
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
        self._update_info: dict = {"available": False, "version": "", "url": "", "notes": ""}
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
                _log(f"app update check: {info['error']}")
            else:
                self._update_info = {k: info[k] for k in ("available", "version", "url", "notes")}
                _log(f"app update: available={info['available']} version={info['version']!r}")
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
        url = (self._update_info or {}).get("url", "")
        if not (url.startswith(("http://", "https://")) and url.lower().endswith(".exe")):
            return {"ok": False, "error": "нет прямой ссылки на установщик"}
        threading.Thread(target=self._do_install_update, args=(url,), daemon=True).start()
        return {"ok": True}

    def _do_install_update(self, url: str) -> None:
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
        }

    def set_setting(self, key: str, value) -> dict:
        if key not in ("autostart", "monitor", "auto_enable", "game_filter", "doh"):
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
        return self.get_settings()

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

    def start_deep_search(self) -> None:
        if self._searching:
            return
        self._searching = True
        self._cancel.clear()
        threading.Thread(target=self._run_deep, daemon=True).start()

    def _run_deep(self) -> None:
        from . import deepsearch
        self._stop_monitors()
        # База для мутаций: текущая стратегия -> лучшая рабочая -> ALT -> первая встроенная.
        base_name = self.strategy_name or (self.working[0]["name"] if self.working else None)
        base = (self._find_strategy(base_name) if base_name else None) or self._find_strategy("ALT")
        if not base:
            base = load_strategies()[0]

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
                base, engine=self.engine, budget=90, stop_on_all=True,
                on_progress=on_progress, on_result=on_result, on_found=on_found,
                cancel=self._cancel,
            )
            _log(f"=== DEEP SEARCH done: кандидатов={len(results)}, "
                 f"рабочих={sum(1 for r in results if r.working)} ===")
        except Exception as e:  # noqa: BLE001
            self._push("onError", f"Ошибка глубокого поиска: {e}")

        working = [_score_to_item(r) for r in results if r.working]
        names = {w["name"] for w in working}
        self.working = working + [w for w in self.working if w["name"] not in names]
        if working:
            # working отсортирован по баллу (services_ok, затем качество голоса) —
            # working[0] это лучшая All с самым быстрым/стабильным голосом.
            self.strategy_name = working[0]["name"]
            self._save()
            self.enable()
        self._searching = False
        self._push("onSearchDone", self._state()["working"])

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
            if sc.working:
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

        working = [_score_to_item(r) for r in results if r.working]
        self.working = working
        if working:
            # results отсортированы по баллу (голос учтён) → working[0] = лучшая.
            self.strategy_name = working[0]["name"]
            self._save()
            # Применяем лучшую стратегию сразу.
            self.enable()
        self._searching = False
        self._push("onSearchDone", working)

    # ---- мониторы (голос по UDP + доступность сервисов по TCP/TLS) ----
    def _start_monitors(self) -> None:
        if self.cfg.get("monitor", True):
            self.monitor.start()
            self.watchdog.start()

    def _stop_monitors(self) -> None:
        self.monitor.stop()
        self.watchdog.stop()

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
        if self._recovering or (now - self._last_recovery) < self.recovery_cooldown:
            return
        # Если проблем давно не было — считаем путь здоровым, сбрасываем счётчик.
        if now - self._last_recovery > self.recover_reset_after:
            self._recover_count = 0
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
        """Переключиться на следующую рабочую стратегию по кругу."""
        names = [w["name"] for w in self.working if w.get("name")]
        if not names:
            # Альтернатив нет — просто перезапустим текущую.
            strat = self._find_strategy(self.strategy_name) if self.strategy_name else None
            if strat:
                _log("recovery: альтернатив нет, перезапуск текущей")
                self._push("onRecovering")
                self.engine.start(strat)
            return
        try:
            i = names.index(self.strategy_name)
        except ValueError:
            i = -1
        nxt = names[(i + 1) % len(names)]
        strat = self._find_strategy(nxt)
        if not strat:
            return
        _log(f"recovery: ПЕРЕКЛЮЧЕНИЕ «{self.strategy_name}» -> «{nxt}»")
        self._push("onRecovering")
        self.engine.start(strat)
        self.strategy_name = nxt
        self._save()
        self._push("onExternalState", self._state())


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
    try:
        (paths.LOG_DIR / "debug.log").write_text("", encoding="utf-8")
    except Exception:
        pass
    _log(f"=== run start (args={args}) ===")
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


if __name__ == "__main__":
    try:
        run()
    except Exception as e:  # noqa: BLE001
        import traceback
        _log("FATAL: " + repr(e) + "\n" + traceback.format_exc())
        raise
