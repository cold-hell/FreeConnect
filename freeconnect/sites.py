"""Свои сайты пользователя в DPI-обход.

winws читает список доменов из hostlist `list-general-user.txt` (он подключён почти
в каждой стратегии). Домены оттуда обрабатываются наравне со встроенными. Даём
человеку добавлять сайты, которые режет его провайдер, а в базовом списке их нет
(частый запрос — «сделайте рабочим сайт X»). Правим только этот файл — базовые
списки остаются нетронутыми.

winws не любит ПУСТОЙ hostlist, поэтому при пустом списке пишем безобидный
плейсхолдер (не совпадает ни с одним реальным доменом).
"""
from __future__ import annotations

import re

from . import paths

USER_LIST = "list-general-user.txt"
HEADER = "# FreeConnect: свои домены для обхода (по одному в строке). Меняется в Настройках → «Свои сайты»."
PLACEHOLDER = "domain.example.abc"   # заглушка, чтобы hostlist не был пустым

# Версия набора дефолтных доменов. Повышаем при изменении набора — тогда при следующем
# запуске дефолты домешиваются, а ретайрнутые (RETIRED_DOMAINS) вычищаются из уже
# существующих списков (см. merge_defaults + Api._seed_default_sites).
SEED_VERSION = 3

# Популярные сервисы, которые чаще всего просят «завести» через DPI-обход в РФ.
# ВАЖНО про формат: zapret-hostlist матчит по СУФФИКСУ — запись `pornhub.com`
# покрывает и `rt.pornhub.com`, и `www.pornhub.com`, и любой `*.pornhub.com`.
# Поэтому перечислять поддомены НЕ нужно; нужен apex сайта + отдельные apex-домены
# CDN (видео/превью часто на других доменах, как у Discord: `phncdn.com` ≠ поддомен
# pornhub.com).
# По взрослым сайтам держим МИНИМУМ — только Pornhub и xHamster (+ их CDN, иначе видео
# не грузится). Остальное порно люди добавляют сами: пихать десятки тьюбов «из коробки»
# — перебор (и не всем нужно). Не-порно домены остаются по умолчанию.
DEFAULT_DOMAINS = [
    # --- Взрослые сайты (минимум: Pornhub + xHamster с их CDN) ---
    "pornhub.com", "pornhubpremium.com", "phncdn.com", "phprcdn.com",
    "xhamster.com", "xhcdn.com",
    # --- Соцсети (душат по SNI; медиа на отдельных CDN) ---
    "twitter.com", "x.com", "twimg.com", "t.co",
    "instagram.com", "cdninstagram.com", "fbcdn.net",
    # --- GitHub (часто придушен; заодно чинит автообновление самого FreeConnect) ---
    "github.com", "githubusercontent.com", "githubassets.com", "github.io",
    # --- Торрент-трекеры (если блочат по DNS — включи ещё «Шифровать DNS») ---
    "rutracker.org", "nnmclub.to", "rutor.info", "rutor.org",
    "kinozal.tv", "1337x.to", "thepiratebay.org",
    # --- Прочее часто-блокируемое ---
    "linkedin.com",
]

# Домены, которые РАНЬШЕ были в дефолтах, а теперь убраны (перебор с порно-тьюбами).
# merge_defaults вычищает их из существующих пользовательских списков при апгрейде
# SEED_VERSION. Если кому-то нужен конкретный сайт — добавит вручную (повторно засев
# уже не сработает, версия одна). Здесь только то, что засевали МЫ; чужое не трогаем,
# кроме совпадений — это осознанно (мы эти порно-домены из дефолтов и ретайрим).
RETIRED_DOMAINS = [
    "trafficjunky.net", "youporn.com", "ypncdn.com", "redtube.com", "rdtcdn.com",
    "tube8.com", "thumbzilla.com", "spankwire.com", "keezmovies.com", "extremetube.com",
    "brazzers.com", "realitykings.com", "mofos.com", "digitalplayground.com", "twistys.com",
    "babes.com", "xvideos.com", "xvideos4.com", "xvideos-cdn.com", "xvcdn.com",
    "xnxx.com", "xnxx-cdn.com", "xhamster19.com", "xhamsterlive.com",
    "spankbang.com", "sb-cd.com", "eporner.com", "txxx.com", "upornia.com", "hclips.com",
    "hdzog.com", "porntrex.com", "youjizz.com", "tnaflix.com", "empflix.com", "drtuber.com",
    "nuvid.com", "sunporno.com", "porn.com", "motherless.com", "beeg.com", "gelbooru.com",
    "rule34.xxx", "chaturbate.com", "stripchat.com", "doppiocdn.com", "bongacams.com",
    "cam4.com", "myfreecams.com", "livejasmin.com", "hanime.tv", "hentaihaven.xxx",
    "nhentai.net", "e-hentai.org",
]

# Домен: 1..253 симв., метки 1..63 из [a-z0-9-], хотя бы одна точка (есть TLD).
_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)(?!-)[a-z0-9-]{1,63}(?:\.(?!-)[a-z0-9-]{1,63})+$")


def _list_path():
    return paths.LISTS_DIR / USER_LIST


def normalize(raw: str) -> str | None:
    """Строка -> чистый домен или None. Терпим к вводу: срезаем схему, путь, www.,
    порт, пробелы; поддомены оставляем. Кириллические домены переводим в punycode."""
    s = (raw or "").strip().lower()
    if not s or s.startswith("#"):
        return None
    s = re.sub(r"^[a-z][a-z0-9+.\-]*://", "", s)   # схема http(s):// и т.п.
    s = s.split("/", 1)[0].split("?", 1)[0]         # путь / query
    s = s.split("@")[-1]                             # user@host
    s = s.split(":", 1)[0]                           # :порт
    s = s.strip().strip(".")
    if s.startswith("www."):
        s = s[4:]
    if not s:
        return None
    if not s.isascii():                             # кириллица/IDN -> punycode
        try:
            s = s.encode("idna").decode("ascii")
        except Exception:  # noqa: BLE001
            return None
    return s if _DOMAIN_RE.match(s) else None


def parse(text: str) -> list[str]:
    """Текст из поля ввода -> список уникальных валидных доменов (порядок сохраняем).
    Разделители — перевод строки, запятая, пробел."""
    out: list[str] = []
    seen: set[str] = set()
    for token in re.split(r"[\s,]+", text or ""):
        d = normalize(token)
        if d and d != PLACEHOLDER and d not in seen:
            seen.add(d)
            out.append(d)
    return out


def read_domains() -> list[str]:
    """Домены, реально записанные в hostlist (без заголовка и плейсхолдера)."""
    try:
        text = _list_path().read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return parse(text)


def write_domains(domains: list[str]) -> int:
    """Перезаписывает hostlist. Пустой список -> плейсхолдер. Возвращает число доменов."""
    paths.ensure_dirs()
    lines = [HEADER]
    lines += domains if domains else [PLACEHOLDER]
    _list_path().write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(domains)


def merge_defaults() -> int:
    """Домешивает дефолтные популярные домены в пользовательский список, НЕ трогая
    прочие добавленные пользователем, и ВЫЧИЩАЕТ ретайрнутые дефолты (RETIRED_DOMAINS —
    например, порно-тьюмы, которые мы перестали засевать). Порядок: сначала дефолты,
    потом прочие пользовательские. Возвращает итоговое число доменов."""
    current = read_domains()
    retired = set(RETIRED_DOMAINS)
    seen, out = set(), []
    for d in DEFAULT_DOMAINS + current:      # дефолты вперёд, дубли убираем
        if d and d not in seen and d not in retired:
            seen.add(d)
            out.append(d)
    write_domains(out)
    return len(out)
