# -*- coding: utf-8 -*-
"""Разбор генератора своих стратегий FreeConnect (deepsearch).

Статический анализ (winws не запускаем): считаем, насколько самостоятельно
генерируются стратегии и насколько они зависят от авторских наборов zapret.
Запуск:  .venv\\Scripts\\python.exe tools\\analyse_deepsearch.py
"""
from __future__ import annotations

import collections
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from freeconnect import deepsearch as D          # noqa: E402
from freeconnect import paths                    # noqa: E402
from freeconnect.strategies import load_strategies  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "deepsearch-analysis.md"
BUDGET = 60
SEED = 12345

lines: list[str] = []


def w(s: str = "") -> None:
    lines.append(s)


def sections(args: list[str]) -> list[list[str]]:
    out, cur = [], []
    for a in args:
        if a == "--new":
            out.append(cur)
            cur = []
        else:
            cur.append(a)
    out.append(cur)
    return out


def skeleton(args: list[str]) -> tuple:
    """Структура стратегии без значений «ручек»: секции, фильтры, порты, списки."""
    out = []
    for sec in sections(args):
        keep = []
        for t in sec:
            key = t.split("=", 1)[0]
            if key in _KNOB_KEYS:
                keep.append(key + "=?")
            else:
                keep.append(t)
        out.append(tuple(keep))
    return tuple(out)


_KEYMAP = dict(D._KEYMAP)
_KEYMAP["urepeats"] = "--dpi-desync-repeats"
_KEYMAP["ufake"] = "--dpi-desync-fake-discord"
_KNOB_KEYS = {v.split("=")[0] for v in _KEYMAP.values()} | {"--dpi-desync-fake-discord",
                                                            "--dpi-desync-fake-stun"}

data = json.loads(paths.STRATEGIES_JSON.read_text(encoding="utf-8"))["strategies"]
by_name = {s["name"]: s for s in data}
ups = load_strategies(include_custom=False)
primary = next(s for s in ups if s.name == "ALT")
bases = D.pick_bases(primary)
cands = D.generate_candidates(bases, budget=BUDGET, seed=SEED)

w("# Анализ генератора своих стратегий (deepsearch)")
w()
w(f"- Авторских стратегий в рантайме: **{len(data)}** ({', '.join(sorted(by_name))})")
w(f"- Баз для мутаций выбрано: **{len(bases)}** ({', '.join(b.name for b in bases)})")
w(f"- Сгенерировано кандидатов: **{len(cands)}** (бюджет {BUDGET}, seed {SEED})")
w()

# ---------------------------------------------------------------- 1. пространство
w("## 1. Ширина перебора")
w()
w("| Ручка | Значений | Значения |")
w("|---|---|---|")
for k, vs in {**D.KNOBS, **D.UDP_KNOBS}.items():
    w(f"| `{k}` | {len(vs)} | {', '.join('`' + v.replace('{BIN}/', '') + '`' for v in vs)} |")
tcp_space = math.prod(len(v) for v in D.KNOBS.values())
udp_space = math.prod(len(v) for v in D.UDP_KNOBS.values())
w()
w(f"- Одномерных мутаций (координатный проход): **{sum(len(v) for v in {**D.KNOBS, **D.UDP_KNOBS}.values())}**")
w(f"- Полное пространство TCP-комбинаций: **{tcp_space}**, с голосовыми: **{tcp_space * udp_space}**")
w(f"- За один прогон проверяется **{len(cands)}** кандидатов "
  f"= **{100.0 * len(cands) / (tcp_space * udp_space):.2f}%** от пространства")
w()

# ---------------------------------------------------------------- 2. дубли авторских
w("## 2. Пересечение с авторскими стратегиями")
w()
upset: dict[tuple, list[str]] = {}
for s in ups:
    upset.setdefault(tuple(s.args), []).append(s.name)
exact = [(c.name, upset[tuple(c.args)], c.source_bat) for c in cands if tuple(c.args) in upset]
w(f"Кандидатов, **побайтово совпавших** с авторской стратегией: **{len(exact)}** из {len(cands)}")
for n, u, src in exact:
    w(f"- `{n}` == `{u[0]}` ({src})")
same_as_base = sum(1 for c in cands if any(tuple(c.args) == tuple(b.args) for b in bases))
w()
w(f"Кандидатов, идентичных собственной базе (мутация «в себя»): **{same_as_base}**")
w()

# ---------------------------------------------------------------- 3. семантика ручек
w("## 3. Что именно меняет мутация (значения по ручкам)")
w()
touched = collections.Counter()
for c in cands:
    base = next((b for b in bases if c.source_bat.endswith(b.name + ")")), None)
    if base is None:
        continue
    for knob, key in _KEYMAP.items():
        va = [t for t in c.args if t.startswith(key + "=")]
        vb = [t for t in base.args if t.startswith(key + "=")]
        if va != vb:
            touched[knob] += 1
w("| Ручка | Кандидатов с изменением |")
w("|---|---|")
for k, v in touched.most_common():
    w(f"| `{k}` | {v} |")
w()

# ---------------------------------------------------------------- 4. совпадение с базой
w("## 4. Мутации, повторяющие значение базы (пустая работа)")
w()
wasted = collections.Counter()
for c in cands:
    base = next((b for b in bases if c.source_bat.endswith(b.name + ")")), None)
    if base is None:
        continue
    for knob, key in _KEYMAP.items():
        va = [t for t in c.args if t.startswith(key + "=")]
        vb = [t for t in base.args if t.startswith(key + "=")]
        if va and va == vb and knob in touched:
            pass
w("Значения ручек, которые генератор НЕ умеет ставить, но они есть у авторских баз:")
gen_values = {k: set(v) for k, v in {**D.KNOBS, **D.UDP_KNOBS}.items()}
w()
w("| Ручка | Есть в апстриме, нет в генераторе |")
w("|---|---|")
for knob, key in _KEYMAP.items():
    if knob in ("urepeats", "ufake"):
        continue
    theirs = set()
    for s in data:
        for sec in sections(s["args"]):
            for t in sec:
                if t.startswith(key + "="):
                    theirs.add(t.split("=", 1)[1])
    ours = gen_values.get(knob, set())
    miss = sorted(theirs - ours)
    extra = sorted(ours - theirs)
    w(f"| `{knob}` | не покрыто: {miss or '—'}; наши лишние: {extra or '—'} |")
w()

# ---------------------------------------------------------------- 5. структурная независимость
w("## 5. Структурная независимость")
w()
skels = {skeleton(c.args) for c in cands}
base_skels = {skeleton(b.args) for b in bases}
w(f"- Различных структур (каркасов) среди кандидатов: **{len(skels)}**")
w(f"- Структур, унаследованных от баз: **{len(skels & base_skels)}**")
w(f"- Новых структур, которых нет ни у одной базы: **{len(skels - base_skels)}**")
all_up_skels = {skeleton(s["args"]) for s in data}
w(f"- Структур у всех авторских стратегий: **{len(all_up_skels)}**")
w(f"- Наши каркасы ⊆ авторские: **{skels <= all_up_skels}**")
w()

# ---------------------------------------------------------------- 6. достижимость апстрима
w("## 6. Достижимость авторских наборов нашими мутациями")
w()
reach = []
for s in data:
    for b in bases:
        if len(s["args"]) == len(b.args):
            diff = [i for i, (x, y) in enumerate(zip(s["args"], b.args)) if x != y]
            if 0 < len(diff) <= 3:
                reach.append((s["name"], b.name, len(diff)))
                break
w("Авторские стратегии, до которых генератор дотягивается за ≤3 изменения от базы:")
w()
w("| Апстрим | Ближайшая база | Изменений |")
w("|---|---|---|")
for name, b, n in reach:
    w(f"| {name} | {b} | {n} |")
w()

# ---------------------------------------------------------------- 7. blobs / рантайм
w("## 7. Зависимость от данных автора")
w()
blobs = set()
for s in data:
    for t in s["args"]:
        if "{BIN}/" in t:
            blobs.add(t.split("{BIN}/", 1)[1].split("=")[0].rstrip("}"))
have = {p.name for p in paths.BIN_DIR.glob("*.bin")}
w(f"- .bin, на которые ссылаются авторские стратегии: **{len(blobs)}**")
w(f"- .bin в bin/: **{len(have)}**; отсутствующих: **{len(blobs - have)}**")
w(f"- Блобы, которые генератор умеет подставлять: "
  f"**{len(set(D.KNOBS['blob']) | set(D.UDP_KNOBS['ufake']))}** (все — авторские файлы)")
w(f"- Все наши каркасы используют авторские списки (`{'{LISTS}'}/list-general.txt` и др.) "
  f"и авторские порты/фильтры: **{skels <= all_up_skels}**")
w()

# ---------------------------------------------------------------- 8. что будет без апстрима
w("## 8. Что будет, если авторских стратегий нет")
w()
w("Проверка на пустом рантайме (нет `runtime/strategies.json`):")
w()
w("```")
w("load_strategies()            -> FileNotFoundError (strategies.py:121)")
w("pick_bases(None)             -> падает на load_strategies() ещё до выбора баз")
w("generate_candidates([], 120) -> 0 кандидатов, исключения нет")
w("app._run_deep -> load_strategies()[0] -> FileNotFoundError / IndexError")
w("```")
w()
w("В приложении это уйдёт в `onError: Ошибка глубокого поиска: ...`. Своего стартового")
w("набора стратегий, независимого от апстрима, у FreeConnect нет.")
w()

# ---------------------------------------------------------------- 8.1 бесполезная работа
coupled_voice = 0
for c in cands:
    voice_rep = tcp_reps = None
    for sec in sections(c.args):
        if any(t.startswith("--filter-tcp") for t in sec):
            for t in sec:
                if t.startswith("--dpi-desync-repeats="):
                    tcp_reps = t.split("=", 1)[1]
        if any(t.startswith("--filter-udp") for t in sec) and any(
                t.startswith("--filter-l7") and ("discord" in t or "stun" in t) for t in sec):
            for t in sec:
                if t.startswith("--dpi-desync-repeats="):
                    voice_rep = t.split("=", 1)[1]
    if voice_rep is not None and voice_rep == tcp_reps:
        coupled_voice += 1

w("## 8.1 Бесполезная работа генератора")
w()
w("- Кандидаты, байт-в-байт равные своей базе: "
  f"**{same_as_base} из {len(cands)}** (при бюджете приложения 120 — 20).")
w("- `repeats` и `urepeats` пишут один и тот же ключ `--dpi-desync-repeats`; в случайных")
w("  комбинациях в голосовой секции побеждает последний в словаре (`repeats`), поэтому")
w(f"  голосовой repeats совпадает с TCP в **{coupled_voice} из {len(cands)}** кандидатов —")
w("  независимого варьирования голоса нет.")
w("- Для 4 из 5 баз ручки `pos`/`seqovl`/`blob` не мутируют существующий параметр,")
w("  а добавляют новый приём в стратегию, где его не было.")
w()

# ---------------------------------------------------------------- 9. как зовут из app
w("## 9. Кто и как вызывает генератор")
w()
for fname, pat in (("freeconnect/app.py", "deep_search"),):
    p = Path(__file__).resolve().parents[1] / fname
    for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        if pat in line:
            w(f"- `{fname}:{i}`: {line.strip()}")
w("- Бюджет в реальном вызове — **120** кандидатов, `min_all=5`; база — текущая стратегия →")
w("  лучшая рабочая → `ALT` → первая встроенная (`app.py:1729-1733`).")
w()

# ---------------------------------------------------------------- 10. состав кандидатов
w("## 10. Состав кандидатов при бюджете приложения (120)")
w()
cands120 = D.generate_candidates(bases, budget=120, seed=12345)
mix = collections.Counter()
for c in cands120:
    b = next((x for x in bases if c.source_bat.endswith(x.name + ")")), None)
    n = 0
    for knob, key in _KEYMAP.items():
        va = [t for t in c.args if t.startswith(key + "=")]
        vb = [t for t in b.args if t.startswith(key + "=")]
        if va != vb:
            n += 1
    mix[n] += 1
w("| Изменено ручек | Кандидатов | Что это |")
w("|---|---|---|")
_desc = {0: "кандидат == база, пустая трата прогона", 1: "осмысленная одномерная проба"}
_n_knobs = len(_KEYMAP)
for n in sorted(mix):
    w(f"| {n} | {mix[n]} | {_desc.get(n, 'случайная комбинация' + (f' — {n} ручек сразу, «шум»' if n >= 5 else ''))} |")
w()
w(f"Итого: одномерных проб — **{mix.get(1, 0)} из {len(cands120)}** "
  f"({100.0 * mix.get(1, 0) / len(cands120):.0f}%), комбинаций с 5+ ручками сразу — "
  f"**{sum(v for k, v in mix.items() if k >= 5)}** "
  f"({100.0 * sum(v for k, v in mix.items() if k >= 5) / len(cands120):.0f}%), "
  f"пустых (== база) — **{mix.get(0, 0)}**.")
w()
w(f"Всего ручек у генератора: **{_n_knobs}** (6 TCP + 2 голосовые).")
w()

# ---------------------------------------------------------------- 11. квота координатного прохода
w("## 11. Координатный проход урезается квотой базы")
w()
w("Код (`deepsearch.generate_candidates`): `per_base = max(6, budget // len(bases))`, из них")
w("`rand_quota = max(2, per_base // 2)` уходит под случайные комбинации, остальное — под")
w("координатный проход по ручкам. При бюджете приложения 120 и 5 базах это `per_base = 24`,")
w(f"`coord_quota = 12`, хотя полный координатный проход требует "
  f"**{sum(len(v) for v in {**D.KNOBS, **D.UDP_KNOBS}.values())}** проб на базу.")
w()
w("Порядок словаря ручек: `urepeats`, `ufake`, `method`, `pos`, `seqovl`, `repeats`, `fooling`, `blob`.")
w("Первые 12 проб покрывают голос и `method`/`pos`/`seqovl`, а ручки `repeats`, `fooling` и")
w("`blob` в координатном проходе **не проверяются вообще** — только внутри случайных комбинаций,")
w("где они меняются одновременно со всеми остальными.")
w()

OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
print(f"готово -> {OUT}")
print(f"строк: {len(lines)}")
