"""
Движок FreeConnect — управление процессом winws.exe.

winws.exe + драйвер WinDivert выполняют сам обход DPI и ТРЕБУЮТ прав администратора.
Движок запускает его скрыто (без окна), гарантированно останавливает чужие
экземпляры winws и конфликтующую службу zapret перед стартом.
"""
from __future__ import annotations

import ctypes
import subprocess
import sys
import time

from . import paths
from .strategies import Strategy

_IS_WIN = sys.platform == "win32"

if _IS_WIN:
    CREATE_NO_WINDOW = 0x08000000
else:  # для линтеров/тестов на не-Windows
    CREATE_NO_WINDOW = 0


class EngineError(Exception):
    pass


def is_admin() -> bool:
    if not _IS_WIN:
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _run_hidden(cmd: list[str], timeout: float = 15.0) -> tuple[int, str]:
    """Служебный запуск консольной команды без окна. Возвращает (код, вывод).

    Вывод декодируется терпимо: системные утилиты Windows пишут в OEM-кодировке,
    поэтому берём байты и декодируем с errors='replace', чтобы не падать на UTF-8.
    """
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=False,  # берём сырые байты, декодируем сами
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW if _IS_WIN else 0,
        )
        out = (proc.stdout or b"") + (proc.stderr or b"")
        return proc.returncode, out.decode("cp866", errors="replace")
    except Exception:
        return 1, ""


def kill_winws() -> None:
    """Останавливает все процессы winws.exe."""
    if not _IS_WIN:
        return
    try:
        _run_hidden(["taskkill", "/F", "/IM", "winws.exe", "/T"])
    except Exception:
        pass


def stop_zapret_service() -> bool:
    """Останавливает службу zapret, если она установлена (конфликт по WinDivert).

    Возвращает True, если служба была найдена и остановлена.
    """
    if not _IS_WIN:
        return False
    try:
        code, out = _run_hidden(["sc", "query", "zapret"])
        if code != 0 or "1060" in out:
            return False  # службы нет
        _run_hidden(["sc", "stop", "zapret"])
        return True
    except Exception:
        return False


class Engine:
    """Держит один активный процесс winws под выбранную стратегию."""

    # pywebview при экспонировании js_api рекурсивно обходит некаллабельные
    # атрибуты объекта Api. Этот флаг говорит ему НЕ заходить внутрь Engine
    # (иначе он лезет в subprocess/Strategy и плодит мусорные api-функции).
    _serializable = False

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self.current: Strategy | None = None
        self._logf = None
        self._winws_log = paths.LOG_DIR / "winws.log"

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _winws_tail(self, n: int = 3) -> str:
        """Последние строки вывода winws — чтобы показать реальную причину падения."""
        try:
            if self._logf:
                self._logf.flush()
            data = self._winws_log.read_bytes().decode("cp866", errors="replace")
            lines = [ln.strip() for ln in data.splitlines() if ln.strip()]
            return " | ".join(lines[-n:])
        except Exception:
            return ""

    def _winws_ready(self) -> bool:
        """winws напечатал, что WinDivert поднят и захват пошёл — можно тестировать.
        Позволяет НЕ досиживать полный settle, если стратегия применилась за <1с."""
        try:
            if self._logf:
                self._logf.flush()
            data = self._winws_log.read_bytes().decode("cp866", errors="replace").lower()
            return "windivert initialized" in data
        except Exception:
            return False

    def start(self, strategy: Strategy, settle: float = 4.0) -> None:
        """Запускает winws со стратегией. settle — пауза на инициализацию (сек)."""
        if not _IS_WIN:
            raise EngineError("Обход поддерживается только на Windows")
        if not is_admin():
            raise EngineError("Нужны права администратора для запуска winws/WinDivert")
        if not paths.WINWS_EXE.is_file():
            raise EngineError(f"winws.exe не найден: {paths.WINWS_EXE}")

        self.stop()
        kill_winws()  # на случай чужих экземпляров

        args = strategy.resolve_args()
        cmd = [str(paths.WINWS_EXE), *args]

        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0  # SW_HIDE

        # Пишем вывод winws в файл — раньше глушили в DEVNULL и не видели ПРИЧИНУ
        # падения («код 1»). Теперь при сбое читаем и показываем реальную ошибку.
        self._winws_log = paths.LOG_DIR / "winws.log"
        try:
            paths.ensure_dirs()
            self._logf = open(self._winws_log, "wb")
        except Exception:
            self._logf = None

        try:
            self._proc = subprocess.Popen(
                cmd,
                cwd=str(paths.BIN_DIR),
                stdout=(self._logf or subprocess.DEVNULL),
                stderr=subprocess.STDOUT,
                startupinfo=startupinfo,
                creationflags=CREATE_NO_WINDOW,
            )
        except Exception as e:  # noqa: BLE001
            raise EngineError(f"Не удалось запустить winws: {e}") from e

        self.current = strategy

        # Ждём инициализации, но выходим сразу в двух случаях:
        #  1) winws упал (кривая мутация завершается мгновенно) — не досиживаем settle;
        #  2) winws напечатал "windivert initialized" — обход уже применён, тестировать
        #     можно сразу (обычно <1с вместо глухих 4с). settle остаётся верхней
        #     границей-фолбэком, если маркера в этой версии нет.
        # Это ускоряет КАЖДУЮ проверку — и автоподбор, и генерацию — не урезая перебор.
        deadline = time.perf_counter() + settle
        while time.perf_counter() < deadline:
            if self._proc.poll() is not None:
                code = self._proc.returncode
                why = self._winws_tail()
                self.current = None
                raise EngineError(f"winws завершился сразу (код {code}) — {why or 'стратегия неприменима'}")
            if self._winws_ready():
                # маленькая доводка, чтобы WinDivert успел навесить callout
                time.sleep(0.25)
                break
            time.sleep(0.1)

        if self._proc.poll() is not None:
            code = self._proc.returncode
            why = self._winws_tail()
            self.current = None
            raise EngineError(f"winws завершился сразу (код {code}) — {why or 'стратегия неприменима'}")

    def stop(self) -> None:
        """Останавливает текущий процесс winws."""
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
        self.current = None
        kill_winws()
