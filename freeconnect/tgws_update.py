"""Проверка свежести ядра обхода Telegram (Flowseal/tg-ws-proxy).

Ядро вендорено в `freeconnect/tgws` и **само себя не обновляет**: код, который
исполняется в процессе приложения, не должен подменяться по сети. Поэтому модуль
только сообщает, что у upstream вышла новая версия, — она попадёт в следующем
релизе FreeConnect. При этом самая изменчивая часть, пул резервных Cloudflare-доменов,
обновляется внутри ядра самостоятельно (раз в час, прямо с их GitHub).

Модуль намеренно лёгкий: один запрос к GitHub Releases с коротким таймаутом,
результат кэшируется в конфиге. Исключений наружу не бросает.
"""
from __future__ import annotations

import json
import re
import time
from urllib.request import Request

from . import tgproxy

REPO = "Flowseal/tg-ws-proxy"
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
CHECK_INTERVAL_H = 12.0   # как часто проверять в фоне
TIMEOUT = 6.0             # короткий: проверка не должна задерживать старт


def _opener():
    """Опенер ядра: он умеет ходить на GitHub по закреплённым адресам с certifi —
    это заметно надёжнее в сетях, где GitHub придушен. Если ядро недоступно,
    откатываемся на обычный urllib."""
    try:
        from .tgws.utils import build_github_opener
        return build_github_opener()
    except Exception:  # noqa: BLE001
        import urllib.request
        return urllib.request.build_opener()


def parse_version(text) -> tuple[int, ...]:
    """Версия -> кортеж чисел. 'v1.11.2' -> (1, 11, 2)."""
    return tuple(int(n) for n in re.findall(r"\d+", str(text or "")))


def is_newer(candidate, current) -> bool:
    """Строго новее ли candidate, чем current (сравниваем по числам)."""
    new, old = parse_version(candidate), parse_version(current)
    if not new:
        return False
    size = max(len(new), len(old))
    new += (0,) * (size - len(new))
    old += (0,) * (size - len(old))
    return new > old


def check(timeout: float = TIMEOUT) -> dict:
    """Спрашивает у GitHub последний релиз ядра. Всегда возвращает словарь:
    {'local', 'latest', 'tag', 'url', 'available', 'error'}."""
    local = tgproxy.core_version()
    out = {"local": local, "latest": "", "tag": "", "url": "",
           "available": False, "error": ""}
    try:
        request = Request(LATEST_URL, headers={
            "User-Agent": "FreeConnect",
            "Accept": "application/vnd.github+json",
        })
        with _opener().open(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out

    tag = str(data.get("tag_name") or "").strip()
    out["tag"] = tag
    out["latest"] = tag.lstrip("v")
    out["url"] = str(data.get("html_url") or "")
    out["available"] = is_newer(out["latest"], local)
    return out


def maybe_check(cfg: dict, interval_h: float = CHECK_INTERVAL_H) -> dict:
    """Проверяет не чаще, чем раз в interval_h часов. Пишет результат в конфиг.
    Возвращает то же, что `check`, плюс флаг 'checked' (делали ли запрос сейчас)."""
    now = time.time()
    last = float(cfg.get("tg_core_checked_at") or 0)
    local = tgproxy.core_version()
    if now - last < interval_h * 3600:
        latest = str(cfg.get("tg_core_latest") or "")
        return {"checked": False, "local": local, "latest": latest, "tag": "",
                "url": "", "available": is_newer(latest, local), "error": ""}

    info = check()
    if not info.get("error"):
        cfg["tg_core_latest"] = info["latest"]
        cfg["tg_core_checked_at"] = now
    return {**info, "checked": True}
