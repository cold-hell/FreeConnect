"""Обновление вендоренного ядра tg-ws-proxy из upstream.

Скачивает файлы пакета `proxy/` из Flowseal/tg-ws-proxy, проверяет каждый файл
по git-blob SHA-1 (content-addressed, берётся из GitHub trees API) и раскладывает
их в `freeconnect/tgws/`, обновляя `UPSTREAM.json`.

Ничего никуда не публикует: только читает GitHub и пишет локальные файлы.
Ручной инструмент разработчика — в приложении не вызывается.

Примеры:
    python tools/update_tgws.py --check              # что у них нового
    python tools/update_tgws.py --tag v1.12.0        # обновить на тег
    python tools/update_tgws.py --branch main        # обновить с ветки main
    python tools/update_tgws.py                      # на последний релиз
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = "Flowseal/tg-ws-proxy"
ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / "freeconnect" / "tgws"
MANIFEST = DEST / "UPSTREAM.json"
UA = {"User-Agent": "FreeConnect-dev"}


def _get(url: str, timeout: float = 60) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _api(path: str):
    return json.loads(_get(f"https://api.github.com/repos/{REPO}{path}").decode("utf-8"))


def latest_tag() -> str:
    return _api("/releases/latest")["tag_name"]


def git_blob_sha(data: bytes) -> str:
    """SHA-1 git-блоба: ровно так GitHub адресует содержимое файла."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def core_files(ref: str):
    """[(путь в репозитории, git-sha, размер)] для файлов пакета proxy/."""
    tree = _api(f"/git/trees/{ref}?recursive=1")
    if tree.get("truncated"):
        raise SystemExit("Дерево репозитория усечено — обновление прервано")
    items = [
        (e["path"], e["sha"], e.get("size", -1))
        for e in tree["tree"]
        if e["type"] == "blob" and e["path"].startswith("proxy/") and e["path"].endswith(".py")
    ]
    if len(items) < 5:
        raise SystemExit(f"Подозрительно мало файлов ядра: {len(items)}")
    return sorted(items)


def fetch_raw(ref: str, path: str) -> bytes:
    return _get(f"https://raw.githubusercontent.com/{REPO}/{ref}/{path}")


def local_state() -> dict:
    if not MANIFEST.is_file():
        return {}
    try:
        return json.loads(MANIFEST.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def cmd_check() -> int:
    state = local_state()
    local = state.get("version", "неизвестно")
    tag = latest_tag()
    print(f"Вендорено: {local} ({state.get('tag', '?')})")
    print(f"У них:     {tag}")
    if tag.lstrip("v") == str(local).lstrip("v"):
        print("Обновление не требуется.")
        return 0
    print("Есть новая версия. Обновить: python tools/update_tgws.py --tag " + tag)
    return 0


def cmd_update(ref: str) -> int:
    print(f"Источник: {REPO} @ {ref}")
    items = core_files(ref)
    print(f"Файлов ядра: {len(items)}")

    staged: dict[str, bytes] = {}
    for path, sha, size in items:
        data = fetch_raw(ref, path)
        actual = git_blob_sha(data)
        if actual != sha:
            raise SystemExit(
                f"Контрольная сумма не совпала для {path}:\n"
                f"  ожидалось {sha}\n  получено  {actual}\n"
                "Файлы не записаны — ничего не изменено."
            )
        staged[path] = data
        print(f"  ok  {path:32} {len(data):>7} б")

    license_data = fetch_raw(ref, "LICENSE")
    init_src = staged["proxy/__init__.py"].decode("utf-8")
    m = re.search(r'__version__\s*=\s*"([^"]+)"', init_src)
    version = m.group(1) if m else "unknown"

    DEST.mkdir(parents=True, exist_ok=True)
    written = []
    for path, data in staged.items():
        target = DEST / Path(path).name
        target.write_bytes(data)
        written.append(target.name)
    (DEST / "LICENSE-tg-ws-proxy.txt").write_bytes(license_data)

    manifest = {
        "repo": REPO,
        "ref": ref,
        "version": version,
        "vendored_at": __import__("time").strftime("%Y-%m-%d"),
        "files": {
            Path(p).name: {"sha256": hashlib.sha256(d).hexdigest(),
                           "git_blob_sha1": git_blob_sha(d),
                           "size": len(d)}
            for p, d in sorted(staged.items())
        },
    }
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    print(f"\nЗаписано файлов: {len(written)}, версия {version}")
    print(f"Манифест: {MANIFEST}")
    print("Дальше: прогнать тесты и проверить Telegram вживую.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Обновление ядра tg-ws-proxy (вендоринг)")
    ap.add_argument("--check", action="store_true", help="только проверить версию")
    ap.add_argument("--tag", metavar="TAG", help="тег upstream, напр. v1.11.2")
    ap.add_argument("--branch", metavar="NAME", help="ветка upstream, напр. main")
    args = ap.parse_args()

    if args.check:
        return cmd_check()
    if args.tag and args.branch:
        raise SystemExit("Укажите что-то одно: --tag или --branch")
    ref = args.tag or args.branch or latest_tag()
    return cmd_update(ref)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except urllib.error.URLError as exc:
        raise SystemExit(f"Сеть недоступна: {exc}") from exc
