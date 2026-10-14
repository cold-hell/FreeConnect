"""
Стартовая диагностика и обслуживание (порт из zapret service.bat: Run Diagnostics
+ обновление списков + проверка обновлений).

Всё выполняется автоматически при запуске под сплэш-экраном. Каждый шаг сообщает
прогресс через колбэк, чтобы UI заряжал молнию и показывал проценты.
"""
from __future__ import annotations

import json
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from typing import Callable

from . import paths

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# Репозиторий, из которого берём свежие списки/версии
UPSTREAM = "Flowseal/zapret-discord-youtube"
RAW_BASE = f"https://raw.githubusercontent.com/{UPSTREAM}/main"
# Списки, которые безопасно обновлять в наш рантайм (домены Discord/YouTube и пр.)
UPDATABLE_LISTS = ["list-general.txt", "list-google.txt"]


@dataclass
class Check:
    name: str
    status: str  # ok | warn | fixed | fail
    detail: str = ""


def _run(cmd: list[str], timeout: float = 15.0) -> tuple[int, str]:
    try:
        p = subprocess.run(
            cmd, capture_output=True, timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
        out = (p.stdout or b"") + (p.stderr or b"")
        return p.returncode, out.decode("cp866", errors="replace")
    except Exception as e:  # noqa: BLE001
        return 1, str(e)


# ---------- проверки окружения (как в service.bat Run Diagnostics) ----------
def check_bfe() -> Check:
    """Base Filtering Engine — обязательная служба для WinDivert/winws."""
    code, out = _run(["sc", "query", "BFE"])
    if "RUNNING" in out.upper():
        return Check("Сетевой фильтр (BFE)", "ok", "служба работает")
    _run(["sc", "start", "BFE"])
    code2, out2 = _run(["sc", "query", "BFE"])
    if "RUNNING" in out2.upper():
        return Check("Сетевой фильтр (BFE)", "fixed", "служба запущена")
    return Check("Сетевой фильтр (BFE)", "fail", "не удалось запустить BFE")


def enable_tcp_timestamps() -> Check:
    """Некоторым стратегиям нужны TCP timestamps — включаем, если выключены."""
    code, out = _run(["netsh", "interface", "tcp", "show", "global"])
    low = out.lower()
    if "timestamps" in low and "enabled" in low.split("timestamps", 1)[1][:40]:
        return Check("Оптимизация TCP", "ok", "timestamps включены")
    r, _ = _run(["netsh", "interface", "tcp", "set", "global", "timestamps=enabled"])
    return Check("Оптимизация TCP", "fixed" if r == 0 else "warn",
                 "timestamps включены" if r == 0 else "не удалось включить timestamps")


def check_conflicts() -> Check:
    """Прокси/Adguard/Killer — частые причины проблем с Discord."""
    problems = []
    # Adguard
    code, out = _run(["tasklist", "/FI", "IMAGENAME eq AdguardSvc.exe"])
    if "AdguardSvc.exe" in out:
        problems.append("Adguard (может мешать Discord)")
    # Killer (сетевые драйверы). Перечисление всех служб медленное — ограничиваем время.
    code, out = _run(["sc", "query", "type=", "service", "state=", "all"], timeout=5.0)
    if "Killer" in out:
        problems.append("Killer сервис")
    # Системный прокси
    try:
        import winreg  # noqa: PLC0415
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
        val, _ = winreg.QueryValueEx(key, "ProxyEnable")
        if val:
            problems.append("включён системный прокси")
        winreg.CloseKey(key)
    except Exception:
        pass
    if problems:
        return Check("Поиск конфликтов", "warn", "; ".join(problems))
    return Check("Поиск конфликтов", "ok", "конфликтов не найдено")


def update_lists() -> Check:
    """Best-effort обновление списков доменов из upstream в рантайм."""
    updated = 0
    for name in UPDATABLE_LISTS:
        try:
            req = urllib.request.Request(f"{RAW_BASE}/lists/{name}",
                                         headers={"User-Agent": "FreeConnect"})
            with urllib.request.urlopen(req, timeout=5) as r:
                if r.status == 200:
                    data = r.read()
                    if data and len(data) > 20:
                        (paths.LISTS_DIR / name).write_bytes(data)
                        updated += 1
        except Exception:
            continue
    if updated:
        return Check("Обновление списков", "ok", f"обновлено файлов: {updated}")
    return Check("Обновление списков", "warn", "не удалось обновить (используем текущие)")


def check_updates() -> Check:
    """Проверка новой версии стратегий на GitHub (пока только уведомление)."""
    try:
        from . import config
        cfg = config.load()
        req = urllib.request.Request(
            f"https://api.github.com/repos/{UPSTREAM}/releases/latest",
            headers={"User-Agent": "FreeConnect", "Accept": "application/vnd.github+json"},
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read().decode("utf-8"))
        tag = data.get("tag_name", "")
        known = cfg.get("upstream_version")
        if tag and tag != known:
            return Check("Проверка обновлений", "warn", f"доступна версия {tag}")
        return Check("Проверка обновлений", "ok", "актуальные стратегии")
    except Exception:
        return Check("Проверка обновлений", "warn", "не удалось проверить")


def update_strategies_step() -> Check:
    """Лёгкий шаг: само обновление стратегий идёт в ФОНЕ (app._bg_update_strategies),
    чтобы 20 сетевых запросов не задерживали автоподключение. Здесь только статус."""
    return Check("Обновление стратегий", "ok", "проверяю Flowseal в фоне")


def load_strategies_step() -> Check:
    from .strategies import load_strategies
    try:
        n = len(load_strategies())
        return Check("Загрузка стратегий", "ok", f"стратегий: {n}")
    except Exception as e:  # noqa: BLE001
        return Check("Загрузка стратегий", "fail", str(e)[:120])


STEPS: list[tuple[str, Callable[[], Check]]] = [
    ("Проверка сетевого драйвера", check_bfe),
    ("Оптимизация TCP", enable_tcp_timestamps),
    ("Поиск конфликтов", check_conflicts),
    ("Обновление списков Discord/YouTube", update_lists),
    ("Обновление стратегий Flowseal", update_strategies_step),
    ("Загрузка стратегий", load_strategies_step),
]


def run_startup(on_step: Callable[..., None]) -> list[Check]:
    """Выполняет все шаги, сообщая прогресс через on_step(i, total, label, check?)."""
    results: list[Check] = []
    total = len(STEPS)
    for i, (label, fn) in enumerate(STEPS):
        on_step(i, total, label, None)
        try:
            c = fn()
        except Exception as e:  # noqa: BLE001
            c = Check(label, "warn", str(e)[:120])
        results.append(c)
        on_step(i + 1, total, label, c)
    return results
