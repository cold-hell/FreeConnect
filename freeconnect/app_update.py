"""
Проверка обновлений самого приложения через GitHub Releases.

Сравнивает локальную версию (__version__) с последним релизом в репозитории и,
если вышла новее, отдаёт ссылку на установщик. Ничего не скачивает и не ставит
автоматически — только показывает баннер «доступно обновление», решает пользователь.

Фолбэк на зеркало: у части пользователей провайдер периодически блокирует GitHub
(сайт не открывается, ассеты не качаются). Поэтому перед проверкой смотрим, доступен
ли GitHub; если нет — берём метаданные и ссылку на установщик с российского зеркала
(SourceCraft). Зеркало отдаёт публичный latest.json АНОНИМНО — токен в приложение не
зашивается (он нужен только нам, локально, чтобы залить установщик на зеркало).
"""
from __future__ import annotations

import json
import re
import urllib.request

from . import __version__

# Репозиторий на GitHub (owner/repo). Заполняется при публикации.
GITHUB_REPO = "cold-hell/FreeConnect"
_API_LATEST = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
# Лёгкая проверка «жив ли GitHub у пользователя» (сам сайт, а не только API —
# провайдер обычно режет по домену целиком).
_GITHUB_PING = "https://github.com"
_UA = {"User-Agent": "FreeConnect", "Accept": "application/vnd.github+json"}

# Зеркало на SourceCraft. У SourceCraft анонимно отдаётся ТОЛЬКО codeload (архивы
# веток/тегов) — raw-файлов и публичных ссылок на вложения релизов нет. Поэтому:
#   - ветка main держит крошечный latest.json (версия + ссылка на архив установщика);
#   - установщик лежит на ветке dist, его codeload-архив и качаем при обновлении.
# MIRROR_LATEST — codeload-архив ветки main (несколько КБ), внутри latest.json:
#   {"version":"v0.1.10","zipball":"<codeload dist>","exe":"FreeConnect-Setup.exe","notes":""}
# Всё анонимно, токен в приложении не нужен.
MIRROR_LATEST = "https://codeload.sourcecraft.tech/millerloves/freeconnect-mirror/zipball/refs/heads/main"


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


def _reachable(url: str, timeout: float = 4.0) -> bool:
    """Быстрая проверка доступности хоста (открывается ли GitHub у пользователя)."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "FreeConnect"}, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except Exception:  # noqa: BLE001
        # HEAD поддерживают не все — пробуем обычный GET, прежде чем сдаться.
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "FreeConnect"})
            with urllib.request.urlopen(req, timeout=timeout):
                return True
        except Exception:  # noqa: BLE001
            return False


def github_reachable(timeout: float = 4.0) -> bool:
    return _reachable(_GITHUB_PING, timeout=timeout)


def _blank() -> dict:
    return {"available": False, "version": "", "url": "", "notes": "", "error": "",
            "source": "", "exe": ""}


def _read_latest_json_from_zip(data: bytes) -> dict | None:
    """Достаёт latest.json из codeload-архива ветки main (zip оборачивает файлы в
    подпапку вида <org>-<repo>-<hash>/latest.json — ищем по имени файла)."""
    import io
    import zipfile
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for m in z.namelist():
            if m == "latest.json" or m.endswith("/latest.json"):
                return json.loads(z.read(m).decode("utf-8"))
    return None


def _check_github(timeout: float) -> dict:
    out = _blank()
    out["source"] = "github"
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


def _check_mirror(timeout: float) -> dict:
    """Читает latest.json с зеркала (codeload-архив ветки main), анонимно."""
    out = _blank()
    out["source"] = "mirror"
    if not MIRROR_LATEST:
        out["error"] = "GitHub недоступен, зеркало не настроено"
        return out
    try:
        req = urllib.request.Request(MIRROR_LATEST, headers={"User-Agent": "FreeConnect"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
        meta = _read_latest_json_from_zip(data)
    except Exception as e:  # noqa: BLE001
        out["error"] = f"зеркало недоступно: {e}"
        return out
    if not meta:
        out["error"] = "на зеркале нет latest.json"
        return out
    tag = (meta.get("version") or "").strip()
    out["version"] = tag
    out["url"] = (meta.get("zipball") or "").strip()   # codeload-архив с установщиком
    out["exe"] = (meta.get("exe") or "FreeConnect-Setup.exe").strip()
    out["notes"] = (meta.get("notes") or "").strip()
    out["available"] = bool(tag) and _is_newer(tag, __version__)
    return out


def check(timeout: float = 8.0) -> dict:
    """Возвращает словарь состояния обновления.

    {available: bool, version: str, url: str, notes: str, error: str, source: str}
    url — прямая ссылка на .exe-установщик (из ассетов релиза или зеркала), иначе на
    страницу релиза. Если GitHub у пользователя недоступен — берём данные с зеркала.
    """
    if github_reachable(timeout=min(4.0, timeout)):
        out = _check_github(timeout)
        if out.get("version"):
            return out
        # GitHub открывается, но версии нет: репозиторий переименован/переехал, сбой
        # API. Молчать нельзя — иначе обновления тихо перестают приходить. Идём на
        # зеркало: оно на другом сервисе и от имени аккаунта на GitHub не зависит.
    # GitHub не открывается у пользователя (блокировка провайдера) — тоже зеркало.
    return _check_mirror(timeout)
