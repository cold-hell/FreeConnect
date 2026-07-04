"""
Генератор strategies.json из батников оригинального zapret.

Читает все `general*.bat` (кроме service.bat) из папки zapret, извлекает
командную строку запуска winws.exe и превращает её в нормализованный список
аргументов с плейсхолдерами {BIN} и {LISTS}. Результат — data/strategies.json.

Запуск:
    python tools/gen_strategies.py [путь_к_zapret] [путь_к_output.json]
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# Game-фильтр -> плейсхолдеры. Реальное значение подставляет Strategy.resolve_args
# по настройке game_filter: ВЫКЛ -> "12" (порт-заглушка, ничего не ловит),
# ВКЛ -> "1024-65535" (как service.bat load_game_filter в zapret).
GAME_DEFAULTS = {
    "%GameFilterTCP%": "{GAME_TCP}",
    "%GameFilterUDP%": "{GAME_UDP}",
    "%GameFilter%": "{GAME_TCP}",
}


def _join_continuations(text: str) -> str:
    """Склеивает строки батника, разорванные символом продолжения '^'."""
    # В .bat '^' в конце строки — продолжение. Убираем '^' + перевод строки.
    return re.sub(r"\^\s*\r?\n", " ", text)


def extract_winws_args(bat_text: str) -> str | None:
    """Возвращает строку аргументов после winws.exe или None, если не найдено."""
    joined = _join_continuations(bat_text)
    # Ищем вызов winws.exe (в кавычках путь), берём всё после закрывающей кавычки exe.
    m = re.search(r'winws\.exe"\s*(.*)', joined)
    if not m:
        return None
    args_line = m.group(1)
    # Отрезаем всё после конца команды (перевод строки, если ещё остался).
    args_line = args_line.split("\r")[0].split("\n")[0]
    return args_line.strip()


def tokenize(args_line: str) -> list[str]:
    """Разбивает строку аргументов на токены с учётом двойных кавычек.

    Кавычки удаляются (пути рантайма без пробелов, subprocess сам заэкранирует).
    """
    tokens: list[str] = []
    cur = []
    in_quote = False
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
    """Подставляет плейсхолдеры {BIN}/{LISTS} и значения game-фильтра."""
    # Пути bin/lists. В батниках: %BIN% и %LISTS% (с завершающим слэшем),
    # либо напрямую %~dp0bin\ / %~dp0lists\.
    token = token.replace("%BIN%", "{BIN}/")
    token = token.replace("%LISTS%", "{LISTS}/")
    token = token.replace("%~dp0bin\\", "{BIN}/")
    token = token.replace("%~dp0lists\\", "{LISTS}/")
    token = token.replace("%~dp0", "{ROOT}/")
    for var, val in GAME_DEFAULTS.items():
        token = token.replace(var, val)
    # Приводим двойные слэши к одинарным (артефакт склейки %BIN%/ + путь).
    token = token.replace("\\", "/")
    token = re.sub(r"/{2,}", "/", token)
    return token


def friendly_name(filename: str) -> str:
    """Человекочитаемое имя стратегии из имени файла."""
    name = filename[:-4] if filename.lower().endswith(".bat") else filename
    # 'general (ALT3)' -> 'ALT3'; 'general' -> 'Default'
    m = re.match(r"^general\s*\((.+)\)$", name, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    if name.lower() == "general":
        return "Default"
    return name


def parse_zapret_folder(zapret_dir: Path) -> list[dict]:
    strategies: list[dict] = []
    bat_files = sorted(
        p for p in zapret_dir.glob("general*.bat")
        if not p.name.lower().startswith("service")
    )
    for bat in bat_files:
        text = bat.read_text(encoding="utf-8", errors="replace")
        args_line = extract_winws_args(text)
        if not args_line:
            print(f"[WARN] winws.exe не найден в {bat.name}, пропуск")
            continue
        tokens = [normalize_token(t) for t in tokenize(args_line)]
        strategies.append({
            "id": bat.stem,               # уникальный id = имя файла без .bat
            "name": friendly_name(bat.name),
            "source_bat": bat.name,
            "args": tokens,
        })
    return strategies


def main() -> int:
    here = Path(__file__).resolve().parent.parent            # FreeConnect/
    default_zapret = here.parent / "zapret"                   # ../zapret
    default_out = here / "data" / "strategies.json"

    zapret_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else default_zapret
    out_path = Path(sys.argv[2]) if len(sys.argv) > 2 else default_out

    if not zapret_dir.is_dir():
        print(f"[ERROR] Папка zapret не найдена: {zapret_dir}")
        return 1

    strategies = parse_zapret_folder(zapret_dir)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps({"strategies": strategies}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[OK] Извлечено стратегий: {len(strategies)} -> {out_path}")
    for s in strategies:
        print(f"  - {s['id']:32} ({len(s['args'])} арг.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
