"""
Автообновление стратегий из upstream (Flowseal/zapret-discord-youtube).

Скачивает свежие `general*.bat`, парсит их в наш формат (плейсхолдеры
{BIN}/{LISTS}/{GAME_TCP}/{GAME_UDP}) и перезаписывает strategies.json — так набор
стратегий не отстаёт от автора, а не является замороженным снапшотом.

Парсер здесь (в пакете), чтобы работал и в собранном .exe. tools/gen_strategies.py
использует его же (единый источник правды).
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request

from . import paths
from .strategies import _sanitize_args

UPSTREAM = "Flowseal/zapret-discord-youtube"
API_CONTENTS = f"https://api.github.com/repos/{UPSTREAM}/contents/"
RAW_BASE = f"https://raw.githubusercontent.com/{UPSTREAM}/main"
_UA = {"User-Agent": "FreeConnect"}

# Game-фильтр -> плейсхолдеры (реальные порты подставляет Strategy.resolve_args).
_GAME = {
    "%GameFilterTCP%": "{GAME_TCP}",
    "%GameFilterUDP%": "{GAME_UDP}",
    "%GameFilter%": "{GAME_TCP}",
}


# ---------- парсер .bat -> args ----------
def _join_continuations(text: str) -> str:
    return re.sub(r"\^\s*\r?\n", " ", text)


def extract_winws_args(bat_text: str) -> str | None:
    joined = _join_continuations(bat_text)
    m = re.search(r'winws\.exe"\s*(.*)', joined)
    if not m:
        return None
    return m.group(1).split("\r")[0].split("\n")[0].strip()


def tokenize(args_line: str) -> list[str]:
    tokens, cur, in_quote = [], [], False
    for ch in args_line:
        if ch == '"':
            in_quote = not in_quote
            continue
        if ch.isspace() and not in_quote:
            if cur:
                tokens.append("".join(cur))
                cur = []
            continue
        cur.append(ch)
    if cur:
        tokens.append("".join(cur))
    return tokens


def normalize_token(token: str) -> str:
    token = (token.replace("%BIN%", "{BIN}/")
                  .replace("%LISTS%", "{LISTS}/")
                  .replace("%~dp0bin\\", "{BIN}/")
                  .replace("%~dp0lists\\", "{LISTS}/")
                  .replace("%~dp0", "{ROOT}/"))
    for var, val in _GAME.items():
        token = token.replace(var, val)
    token = token.replace("\\", "/")
    token = re.sub(r"/{2,}", "/", token)
    return token


def friendly_name(filename: str) -> str:
    name = filename[:-4] if filename.lower().endswith(".bat") else filename
    m = re.match(r"^general\s*\((.+)\)$", name, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return "Default" if name.lower() == "general" else name


def parse_bat_text(bat_text: str, filename: str) -> dict | None:
    """Один .bat -> словарь стратегии (или None, если winws не найден)."""
    args_line = extract_winws_args(bat_text)
    if not args_line:
        return None
    tokens = _sanitize_args([normalize_token(t) for t in tokenize(args_line)])
    stem = filename[:-4] if filename.lower().endswith(".bat") else filename
    return {"id": stem, "name": friendly_name(filename),
            "source_bat": filename, "args": tokens}


# ---------- фетч из upstream ----------
def _fetch_bat_names(timeout: float = 15.0) -> list[str]:
    req = urllib.request.Request(API_CONTENTS,
                                 headers={**_UA, "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        items = json.load(r)
    return sorted(
        i["name"] for i in items
        if i.get("type") == "file"
        and i["name"].lower().endswith(".bat")
        and i["name"].lower().startswith("general")
    )


def _fetch_bat(name: str, timeout: float = 15.0, attempts: int = 3) -> str:
    url = f"{RAW_BASE}/{urllib.parse.quote(name)}"
    last = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers=_UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(0.5 * (i + 1))  # разовые сетевые hiccup'ы -> не терять стратегию
    raise last if last else RuntimeError("fetch failed")


def update_from_upstream(min_ok: int = 10, timeout: float = 12.0) -> tuple[int, str]:
    """Скачивает и перезаписывает strategies.json. Возвращает (кол-во, ошибка).

    Файлы качаем ПАРАЛЛЕЛЬНО (иначе 20 последовательных запросов вешают старт).
    Пишем ТОЛЬКО если распарсили >= min_ok стратегий (защита от пустого/битого ответа).
    """
    from concurrent.futures import ThreadPoolExecutor
    try:
        names = _fetch_bat_names(timeout=timeout)
    except Exception as e:  # noqa: BLE001
        return 0, f"листинг не получен: {e}"

    def _one(name: str) -> dict | None:
        try:
            s = parse_bat_text(_fetch_bat(name, timeout=timeout), name)
            return s if (s and s["args"]) else None
        except Exception:
            return None

    strategies: list[dict] = []
    with ThreadPoolExecutor(max_workers=min(8, len(names) or 1)) as ex:
        for s in ex.map(_one, names):
            if s:
                strategies.append(s)
    strategies.sort(key=lambda s: s["id"])
    if len(strategies) < min_ok:
        return 0, f"распарсено мало стратегий ({len(strategies)}) — не перезаписываю"
    try:
        paths.ensure_dirs()
        tmp = paths.STRATEGIES_JSON.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"strategies": strategies}, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(paths.STRATEGIES_JSON)  # атомарная замена
    except Exception as e:  # noqa: BLE001
        return 0, f"запись не удалась: {e}"
    return len(strategies), ""


def maybe_update(min_interval_hours: float = 24.0) -> tuple[int, str]:
    """Обновляет не чаще раза в min_interval_hours (метка в config)."""
    from . import config
    cfg = config.load()
    last = cfg.get("strategies_updated_at", 0) or 0
    if time.time() - last < min_interval_hours * 3600:
        return 0, "недавно обновляли — пропуск"
    n, err = update_from_upstream()
    if n:
        cfg["strategies_updated_at"] = time.time()
        config.save(cfg)
    return n, err
