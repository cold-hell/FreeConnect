"""
Движок VPN-для-Discord — управление процессом sing-box.exe (TUN + маршрут
«процессы Discord → VPN, остальное → direct»). См. [[freeconnect]] план VPN-фолбэка.

Отдельный процесс, НЕ пересекается с winws:
- sing-box заворачивает трафик Discord в туннель к твоему VPN-серверу; наружу летят
  пакеты туннеля к server:port (обычно НЕ 443 — Hysteria2 8447, grpc 2087, vless 444/445),
  а winws-десинк фильтрует в основном 443/QUIC — поэтому они не мешают друг другу, и
  явно исключать IP сервера из winws в MVP не требуется;
- остальной трафик (не-Discord) идёт `direct` и продолжает десинкаться winws как раньше.

TUN требует прав администратора (они у нас уже есть). Всё best-effort: не поднялся —
логируем причину и выключаемся, обход winws при этом не страдает.
"""
from __future__ import annotations

import json
import subprocess
import sys

from . import paths
from .engine import CREATE_NO_WINDOW, _IS_WIN, _run_hidden, is_admin

SINGBOX_EXE = paths.BIN_DIR / "sing-box.exe"
SINGBOX_CONFIG = paths.RUNTIME_DIR / "singbox.json"


class SingBoxError(Exception):
    pass


def kill_singbox() -> None:
    """Гасит все процессы sing-box.exe (чужие/зависшие)."""
    if not _IS_WIN:
        return
    try:
        _run_hidden(["taskkill", "/F", "/IM", "sing-box.exe", "/T"])
    except Exception:
        pass


class SingBox:
    """Держит один процесс sing-box под выбранный VPN-сервер."""

    _serializable = False   # pywebview не должен обходить объект при сборке js_api

    def __init__(self, log=None) -> None:
        self._proc: subprocess.Popen | None = None
        self._logf = None
        self._log = log or (lambda _m: None)
        self._sb_log = paths.LOG_DIR / "singbox.log"

    def available(self) -> bool:
        """Забандлен ли бинарник sing-box (иначе фича недоступна)."""
        return SINGBOX_EXE.is_file()

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def write_config(self, config: dict) -> None:
        paths.ensure_dirs()
        SINGBOX_CONFIG.write_text(json.dumps(config, ensure_ascii=False, indent=2),
                                  encoding="utf-8")

    def _cmd(self) -> list[str]:
        # sing-box run -c <config>  (рабочая директория — bin, там бинарник и рантайм)
        return [str(SINGBOX_EXE), "run", "-c", str(SINGBOX_CONFIG)]

    def tail(self, n: int = 5) -> str:
        try:
            if self._logf:
                self._logf.flush()
            data = self._sb_log.read_text(encoding="utf-8", errors="replace")
            lines = [ln.strip() for ln in data.splitlines() if ln.strip()]
            return " | ".join(lines[-n:])
        except Exception:
            return ""

    def start(self, config: dict, settle: float = 2.5) -> None:
        """Пишет конфиг и запускает sing-box. Бросает SingBoxError при недоступности/сбое."""
        if not _IS_WIN:
            raise SingBoxError("VPN поддерживается только на Windows")
        if not self.available():
            raise SingBoxError("sing-box не установлен (обнови приложение)")
        if not is_admin():
            raise SingBoxError("Нужны права администратора для TUN")

        self.stop()
        kill_singbox()   # на случай чужих экземпляров
        self.write_config(config)

        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0  # SW_HIDE
        try:
            self._logf = open(self._sb_log, "wb")
        except Exception:
            self._logf = None
        try:
            self._proc = subprocess.Popen(
                self._cmd(),
                cwd=str(paths.BIN_DIR),
                stdout=(self._logf or subprocess.DEVNULL),
                stderr=subprocess.STDOUT,
                startupinfo=startupinfo,
                creationflags=CREATE_NO_WINDOW,
            )
        except Exception as e:  # noqa: BLE001
            raise SingBoxError(f"Не удалось запустить sing-box: {e}") from e

        # Короткая проверка: если процесс сразу упал (кривой конфиг/сервер) — покажем причину.
        import time
        deadline = time.perf_counter() + settle
        while time.perf_counter() < deadline:
            if self._proc.poll() is not None:
                why = self.tail()
                self._proc = None
                raise SingBoxError(f"sing-box завершился сразу — {why or 'проверь конфиг/сервер'}")
            time.sleep(0.15)
        self._log(f"singbox: запущен ({self.tail(1) or 'ok'})")

    def stop(self) -> None:
        if self._proc is not None:
            try:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
            except Exception:
                pass
            self._proc = None
        if self._logf is not None:
            try:
                self._logf.close()
            except Exception:
                pass
            self._logf = None
        kill_singbox()
