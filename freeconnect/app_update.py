"""
Проверка обновлений самого приложения через GitHub Releases.

Сравнивает локальную версию (__version__) с последним релизом в репозитории и,
если вышла новее, отдаёт ссылку на установщик. Ничего не скачивает и не ставит
автоматически — только показывает баннер «доступно обновление», решает пользователь.
"""
from __future__ import annotations

import json
import re
import urllib.request

from . import __version__

# Репозиторий на GitHub (owner/repo). Заполняется при публикации.
GITHUB_REPO = "adolfloves/FreeConnect"
_API_LATEST = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
_UA = {"User-Agent": "FreeConnect", "Accept": "application/vnd.github+json"}


def _parse_ver(tag: str) -> tuple[int, ...]:
    """'v1.2.3' / '1.2.3' -> (1, 2, 3). Нечисловые хвосты игнорируются."""
    nums = re.findall(r"\d+", tag or "")
    return tuple(int(n) for n in nums) if nums else (0,)


def _is_newer(remote: str, local: str) -> bool:
    r, l = _parse_ver(remote), _parse_ver(local)
    n = max(len(r), len(l))
    r += (0,) * (n - len(r))
    l += (0,) * (n - len(l))
    return r > l


def check(timeout: float = 8.0) -> dict:
    """Возвращает словарь состояния обновления.

    {available: bool, version: str, url: str, notes: str, error: str}
    url — прямая ссылка на .exe-установщик из ассетов релиза (если есть),
    иначе на страницу релиза.
    """
    out = {"available": False, "version": "", "url": "", "notes": "", "error": ""}
    if not GITHUB_REPO or "__" in GITHUB_REPO:
        out["error"] = "репозиторий не настроен"
        return out
    try:
        req = urllib.request.Request(_API_LATEST, headers=_UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
    except Exception as e:  # noqa: BLE001
        out["error"] = f"проверка не удалась: {e}"
        return out

    tag = data.get("tag_name") or data.get("name") or ""
    out["version"] = tag
    out["notes"] = (data.get("body") or "").strip()
    # прямая ссылка на установщик, если приложен как ассет
    page_url = data.get("html_url", "")
    asset_url = ""
    for a in data.get("assets", []) or []:
        name = (a.get("name") or "").lower()
        if name.endswith(".exe") and ("setup" in name or "install" in name):
            asset_url = a.get("browser_download_url", "")
            break
    if not asset_url:
        for a in data.get("assets", []) or []:
            if (a.get("name") or "").lower().endswith(".exe"):
                asset_url = a.get("browser_download_url", "")
                break
    out["url"] = asset_url or page_url
    out["available"] = bool(tag) and _is_newer(tag, __version__)
    return out
