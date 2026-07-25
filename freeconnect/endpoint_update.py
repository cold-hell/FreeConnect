"""
Доставка живых адресов веб-входа Telegram через наш канал обновлений.

Зачем. Обход Telegram держится на «живых» IP узлов kws*.web.telegram.org: DNS
отдаёт заблокированные адреса, поэтому рабочие зашиты в tgproxy.DC_ENDPOINTS.
Если такой адрес однажды заблокируют, сломается сразу у всех. Автопоиск
(tgproxy.discover) чинит это на машине пользователя, но требует его действия и
нескольких минут. Этот модуль — быстрый централизованный путь: мы публикуем новый
адрес, и приложения подхватывают его сами при следующем запуске.

Два канала, как у обновлений приложения (см. app_update):
  - GitHub: обычный raw-файл в нашем репозитории;
  - зеркало SourceCraft: тот же файл внутри codeload-архива ветки main (там же,
    где latest.json) — на случай, когда провайдер режет GitHub.

Формат файла:
    {"updated": "2026-07-21",
     "endpoints": {"2": ["149.154.167.220"], "4": ["149.154.167.220"]},
     "cf_domains": ["example.com"]}

`endpoints` — живые IP веб-входа (в обход DNS). `cf_domains` — резервные Cloudflare-
домены (за ними релей на веб-вход Telegram): подключаемся к kws{dc}.{домен} через
обычный DNS, когда прямые IP придушат. Оба поля публикуются здесь, чтобы чинить
блокировку удалённо, без пересборки приложения.

Данные из сети НЕ заменяют локальные, а объединяются: у пользователя может быть свой
найденный адрес, который работает именно у его провайдера.
"""
from __future__ import annotations

import json
import re
import time
import urllib.request

from .app_update import GITHUB_REPO, MIRROR_LATEST, github_reachable

ENDPOINTS_PATH = "data/tg_endpoints.json"
GITHUB_RAW = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/{ENDPOINTS_PATH}"
_UA = {"User-Agent": "FreeConnect"}

MAX_PER_DC = 5          # длинный список дорог: каждый мёртвый адрес — это таймаут
MAX_CF_DOMAINS = 8
MIN_INTERVAL_HOURS = 6.0
CF_MIN_INTERVAL_HOURS = 12.0

_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)(?!-)[a-z0-9-]{1,63}(?:\.(?!-)[a-z0-9-]{1,63})+$")


def _valid(data) -> dict:
    """Оставляет только осмысленное: {номер ДЦ (строкой): [IPv4, ...]}."""
    import ipaddress
    out: dict[str, list[str]] = {}
    if not isinstance(data, dict):
        return out
    for dc, ips in (data.get("endpoints") or {}).items():
        try:
            key = str(int(dc))
        except (TypeError, ValueError):
            continue
        if not isinstance(ips, list):
            continue
        good = []
        for ip in ips:
            try:
                ipaddress.IPv4Address(str(ip))
            except Exception:  # noqa: BLE001
                continue
            if ip not in good:
                good.append(str(ip))
        if good:
            out[key] = good[:MAX_PER_DC]
    return out


def _valid_cf(data) -> list[str]:
    """Оставляет только валидные доменные имена из cf_domains (base-домены без kws-префикса)."""
    out: list[str] = []
    if not isinstance(data, dict):
        return out
    for d in (data.get("cf_domains") or []):
        if not isinstance(d, str):
            continue
        d = d.strip().lower()
        if _DOMAIN_RE.match(d) and d not in out:
            out.append(d)
    return out[:MAX_CF_DOMAINS]


def _fetch_raw(timeout: float) -> tuple[dict | None, str]:
    """Сырой JSON публикуемого файла: GitHub основной, зеркало — фолбэк.
    Возвращает (данные или None, ошибка)."""
    if not MIRROR_LATEST and (not GITHUB_REPO or "__" in GITHUB_REPO):
        return None, "каналы не настроены"
    errors = []
    if github_reachable(timeout=4.0):
        try:
            req = urllib.request.Request(GITHUB_RAW, headers=_UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8")), ""
        except Exception as e:  # noqa: BLE001
            errors.append(f"github: {e}")
    else:
        errors.append("github недоступен")
    try:
        raw = _mirror_raw(timeout)
        if raw is not None:
            return raw, ""
        errors.append("зеркало: файла нет")
    except Exception as e:  # noqa: BLE001
        errors.append(f"зеркало: {e}")
    return None, "; ".join(errors)


def _mirror_raw(timeout: float) -> dict | None:
    """Сырой JSON из codeload-архива ветки main (zip кладёт файлы в подпапку)."""
    import io
    import zipfile
    req = urllib.request.Request(MIRROR_LATEST, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for m in z.namelist():
            if m == ENDPOINTS_PATH or m.endswith("/" + ENDPOINTS_PATH):
                return json.loads(z.read(m).decode("utf-8"))
    return None


def fetch_remote(timeout: float = 10.0) -> tuple[dict, str]:
    """Забирает опубликованные адреса. Возвращает (endpoints, ошибка).
    GitHub основной; если он у пользователя недоступен — зеркало."""
    raw, err = _fetch_raw(timeout)
    if raw is None:
        return {}, err
    eps = _valid(raw)
    if eps:
        return eps, ""
    return {}, err or "в файле нет адресов"


def fetch_cf_domains(timeout: float = 10.0) -> tuple[list[str], str]:
    """Забирает опубликованные резервные Cloudflare-домены. Возвращает (домены, ошибка)."""
    raw, err = _fetch_raw(timeout)
    if raw is None:
        return [], err
    doms = _valid_cf(raw)
    if doms:
        return doms, ""
    return [], err or "в файле нет cf_domains"


def merge(local: dict | None, remote: dict) -> dict:
    """Объединяет опубликованные адреса с локальными.

    Свежеопубликованные идут первыми (их мы только что проверили), локальные —
    следом: у пользователя мог найтись свой рабочий адрес, терять его нельзя."""
    out = dict(local or {})
    for dc, ips in remote.items():
        merged = list(ips)
        for ip in out.get(dc, []) or []:
            if ip not in merged:
                merged.append(ip)
        out[dc] = merged[:MAX_PER_DC]
    return out


def maybe_update(min_interval_hours: float = MIN_INTERVAL_HOURS) -> tuple[dict, str]:
    """Не чаще раза в min_interval_hours подмешивает опубликованные адреса в конфиг.
    Возвращает (новая таблица или {}, ошибка/причина пропуска)."""
    from . import config
    cfg = config.load()
    last = cfg.get("tg_endpoints_updated_at", 0) or 0
    if time.time() - last < min_interval_hours * 3600:
        return {}, "недавно обновляли — пропуск"

    remote, err = fetch_remote()
    if not remote:
        return {}, err or "пусто"

    merged = merge(cfg.get("tg_endpoints") or {}, remote)
    cfg["tg_endpoints_updated_at"] = time.time()
    if merged != (cfg.get("tg_endpoints") or {}):
        cfg["tg_endpoints"] = merged
        config.save(cfg)
        return merged, ""
    config.save(cfg)     # запоминаем время проверки, даже если не изменилось
    return {}, "адреса не изменились"


def maybe_update_cf_domains(min_interval_hours: float = CF_MIN_INTERVAL_HOURS) -> tuple[list[str], str]:
    """Не чаще раза в min_interval_hours подтягивает резервные Cloudflare-домены в
    конфиг (tg_cf_domains). Возвращает (новый список или [], ошибка/причина пропуска)."""
    from . import config
    cfg = config.load()
    last = cfg.get("tg_cf_updated_at", 0) or 0
    if time.time() - last < min_interval_hours * 3600:
        return [], "недавно обновляли — пропуск"

    doms, err = fetch_cf_domains()
    cfg["tg_cf_updated_at"] = time.time()
    if not doms:
        config.save(cfg)     # запоминаем время проверки
        return [], err or "пусто"
    if doms != (cfg.get("tg_cf_domains") or []):
        cfg["tg_cf_domains"] = doms
        config.save(cfg)
        return doms, ""
    config.save(cfg)
    return [], "домены не изменились"
