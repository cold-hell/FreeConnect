"""
Глубокий поиск — генерация собственных стратегий на основе рабочей базы.

Идея (как и просил пользователь): берём рабочую базу (например ALT), мутируем
ключевые «ручки» DPI-десинка winws, тестируем каждую комбинацию вживую и сохраняем
рабочие как «FreeConnect #N». Так программа не зависит от автора zapret.

Мы НЕ знаем внутренностей ТСПУ — просто эмпирически измеряем, что проходит сейчас.
Некорректные комбинации winws отсеиваются сами: процесс мгновенно завершается и
кандидат получает 0 (см. autosearch.evaluate_strategy).
"""
from __future__ import annotations

import random
import threading
from typing import Callable

from . import custom
from .autosearch import StrategyScore, evaluate_strategy
from .engine import Engine
from .strategies import Strategy

# Значения «ручек» для перебора. Подобраны из практики zapret-стратегий.
KNOBS: dict[str, list[str]] = {
    "method":  ["multisplit", "multidisorder", "fake,multisplit", "fake,multidisorder"],
    "pos":     ["1", "2", "3", "host+1", "midsld", "sniext+2"],
    "seqovl":  ["1", "336", "568", "652", "681"],
    "repeats": ["2", "4", "6", "8", "11"],
    "fooling": ["", "badseq", "badsum", "md5sig", "datanoack"],
    "blob":    ["{BIN}/tls_clienthello_www_google_com.bin",
                "{BIN}/tls_clienthello_4pda_to.bin",
                "{BIN}/tls_clienthello_max_ru.bin"],
}

# Соответствие ручки -> ключ аргумента winws
_KEYMAP = {
    "method":  "--dpi-desync",
    "pos":     "--dpi-desync-split-pos",
    "seqovl":  "--dpi-desync-split-seqovl",
    "repeats": "--dpi-desync-repeats",
    "fooling": "--dpi-desync-fooling",
    "blob":    "--dpi-desync-split-seqovl-pattern",
}

# Ручки для ГОЛОСОВОЙ UDP-секции Discord (--filter-l7=discord,stun). Раньше
# генератор её не трогал вовсе, поэтому свои стратегии наследовали голос базы
# один в один: сдохла база — сдохли все. Теперь крутим и голос, а валидатор
# (tester.check_voice) отсеивает мутации, где голос не поднялся.
UDP_KNOBS: dict[str, list[str]] = {
    "urepeats": ["2", "4", "6", "8", "11"],
    "ufake":    ["{BIN}/quic_initial_dbankcloud_ru.bin",
                 "{BIN}/quic_initial_www_google_com.bin",
                 "{BIN}/stun.bin"],
}
# Ключи, на которые ufake ставит один и тот же блоб (discord + stun вместе).
_UDP_FAKE_KEYS = ("--dpi-desync-fake-discord", "--dpi-desync-fake-stun")


def _split_sections(args: list[str]) -> list[list[str]]:
    sections, cur = [], []
    for a in args:
        if a == "--new":
            sections.append(cur)
            cur = []
        else:
            cur.append(a)
    sections.append(cur)
    return sections


def _join_sections(sections: list[list[str]]) -> list[str]:
    out: list[str] = []
    for i, sec in enumerate(sections):
        if i:
            out.append("--new")
        out.extend(sec)
    return out


def _set_kv(section: list[str], key: str, value: str) -> None:
    """Заменяет/добавляет токен key=value (точное совпадение ключа)."""
    prefix = key + "="
    for i, t in enumerate(section):
        if t.startswith(prefix):
            section[i] = prefix + value
            return
    section.append(prefix + value)


def _remove_key(section: list[str], key: str) -> None:
    prefix = key + "="
    section[:] = [t for t in section if not t.startswith(prefix)]


def _is_tcp_section(section: list[str]) -> bool:
    return any(t.startswith("--filter-tcp") for t in section)


def _is_voice_udp_section(section: list[str]) -> bool:
    """Голосовая UDP-секция Discord: есть --filter-udp и l7-фильтр discord/stun."""
    has_udp = any(t.startswith("--filter-udp") for t in section)
    has_l7 = any(t.startswith("--filter-l7") and ("discord" in t or "stun" in t)
                 for t in section)
    return has_udp and has_l7


def mutate(base_args: list[str], knobs: dict[str, str]) -> list[str]:
    """Применяет ручки: TCP-ручки к TCP-секциям, UDP-ручки к голосовой секции.

    Ключи из _KEYMAP идут в TCP-секции; ключи из UDP_KNOBS (urepeats/ufake) —
    в голосовую UDP-секцию Discord. Прочие UDP/QUIC-секции (видео, general)
    не трогаем, чтобы не ломать сторонний UDP-трафик.
    """
    tcp_knobs = {k: v for k, v in knobs.items() if k in _KEYMAP}
    udp_knobs = {k: v for k, v in knobs.items() if k in UDP_KNOBS}
    sections = _split_sections(base_args)
    for sec in sections:
        if _is_tcp_section(sec):
            for knob, value in tcp_knobs.items():
                key = _KEYMAP[knob]
                if knob == "fooling" and value == "":
                    _remove_key(sec, key)
                else:
                    _set_kv(sec, key, value)
        elif udp_knobs and _is_voice_udp_section(sec):
            for knob, value in udp_knobs.items():
                if knob == "urepeats":
                    _set_kv(sec, "--dpi-desync-repeats", value)
                elif knob == "ufake":
                    for fk in _UDP_FAKE_KEYS:
                        _set_kv(sec, fk, value)
    return _join_sections(sections)


# Разнообразные базы-структуры (разные семейства приёмов). Даже если НИ ОДНА
# авторская стратегия не работает как есть, мутации поверх нескольких структур
# сильно расширяют охват — так мы не привязаны к одной рабочей базе.
FALLBACK_BASES = ["ALT", "ALT3", "ALT9", "SIMPLE FAKE", "FAKE TLS AUTO", "ALT2"]


def pick_bases(primary: Strategy | None) -> list[Strategy]:
    from .strategies import load_strategies
    by_name = {s.name: s for s in load_strategies(include_custom=False)}
    bases: list[Strategy] = []
    if primary:
        bases.append(primary)
    for name in FALLBACK_BASES:
        s = by_name.get(name)
        if s and all(s.name != b.name for b in bases):
            bases.append(s)
    if not bases:
        bases = list(by_name.values())[:5]
    return bases[:5]


def generate_candidates(bases: list[Strategy], budget: int, seed: int = 0) -> list[Strategy]:
    """Генерирует кандидатов из НЕСКОЛЬКИХ баз: координатный проход + случайные."""
    rng = random.Random(seed or None)
    seen: set[tuple[str, ...]] = set()
    out: list[Strategy] = []
    per_base = max(6, budget // max(1, len(bases)))

    def _add(base: Strategy, knobs: dict[str, str]) -> None:
        args = mutate(base.args, knobs)
        key = tuple(args)
        if key in seen:
            return
        seen.add(key)
        out.append(Strategy(id=f"cand_{len(out)}", name=f"Кандидат {len(out) + 1}",
                            source_bat=f"deepsearch(base={base.name})", args=args))

    # Голосовые ручки идут ПЕРВЫМИ в координатном проходе, чтобы точно попасть в
    # квоту (иначе TCP-ручки её выбирали и голос никогда не менялся).
    all_knobs = {**UDP_KNOBS, **KNOBS}
    for base in bases:
        start = len(out)
        # Часть квоты резервируем под случайные комбинации — они крутят TCP и
        # голос одновременно, гарантируя покрытие голосовых мутаций.
        rand_quota = max(2, per_base // 2)
        coord_quota = per_base - rand_quota
        # 1) координатный проход — по одной ручке за раз (сначала голосовые)
        for knob, values in all_knobs.items():
            for v in values:
                if len(out) - start >= coord_quota or len(out) >= budget:
                    break
                _add(base, {knob: v})
        # 2) случайные комбинации до квоты базы (TCP + голос сразу)
        guard = 0
        while len(out) - start < per_base and len(out) < budget and guard < per_base * 20:
            guard += 1
            _add(base, {k: rng.choice(vs) for k, vs in all_knobs.items()})

    return out[:budget]


def deep_search(
    base: Strategy,
    services: list[str] | None = None,
    budget: int = 60,
    timeout: float = 5.0,
    settle: float = 4.0,
    engine: Engine | None = None,
    on_progress: Callable[[int, int, Strategy], None] | None = None,
    on_result: Callable[[StrategyScore], None] | None = None,
    on_found: Callable[[Strategy, StrategyScore], None] | None = None,
    cancel: threading.Event | None = None,
    stop_after: int = 0,
    stop_on_all: bool = True,
    max_youtube_only: int = 1,
) -> list[StrategyScore]:
    """Перебирает сгенерированных кандидатов, сохраняя рабочих как FreeConnect #N.

    ПРИОРИТЕТ — Discord (голос). Логика сохранения/остановки (по требованию юзера):
      - сохраняем кандидата с рабочим Discord (метка Discord/All) всегда;
      - ютуб-онли сохраняем максимум `max_youtube_only` штук (как запасную), чтобы
        не засорять список и не «останавливаться» на ютубе;
      - `stop_on_all` — как только найден кандидат с ВСЕМИ сервисами (Discord+голос
        И YouTube) — цель достигнута, останавливаемся;
      - `stop_after` — доп. лимит по числу сохранённых Discord/All (0 = без лимита).
    """
    from . import tester
    svcs = services or list(tester.DEFAULT_TARGETS.keys())
    bases = pick_bases(base)              # мутируем несколько структур, не одну
    candidates = generate_candidates(bases, budget)
    eng = engine or Engine()
    results: list[StrategyScore] = []
    found_discord = 0
    yt_only_saved = 0
    total = len(candidates)

    try:
        for i, cand in enumerate(candidates):
            if cancel is not None and cancel.is_set():
                break
            if on_progress:
                on_progress(i, total, cand)
            sc = evaluate_strategy(eng, cand, svcs, timeout=timeout, settle=settle)
            results.append(sc)

            ok_services = sc.working_services
            disc_ok = "discord" in ok_services
            all_ok = sc.services_ok == len(svcs) and len(svcs) > 0

            # решаем, сохранять ли (приоритет — Discord; ютуб-онли лимитируем)
            keep = False
            if sc.working:
                if disc_ok:
                    keep = True
                elif yt_only_saved < max_youtube_only:
                    keep = True
                    yt_only_saved += 1
            if keep:
                saved = custom.add_custom(cand.args, base_name=(base.name if base else "auto"),
                                          label=sc.result_label())
                sc.strategy = saved
                if disc_ok:
                    found_discord += 1
                if on_found:
                    on_found(saved, sc)
            if on_result:
                on_result(sc)

            if stop_on_all and all_ok:
                break                       # нашли Discord+голос+YouTube — цель достигнута
            if stop_after and found_discord >= stop_after:
                break
    finally:
        eng.stop()

    results.sort(key=lambda s: s.score, reverse=True)
    return results
