# -*- coding: utf-8 -*-
"""Быстрая проверка генератора (без winws): состав и порядок кандидатов."""
import collections
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from freeconnect import deepsearch as D          # noqa: E402
from freeconnect.strategies import load_strategies  # noqa: E402

ups = load_strategies(include_custom=False)
primary = next(s for s in ups if s.name == "ALT")
bases = D.pick_bases(primary)
print("базы (старый pick_bases):", [b.name for b in bases])
print("базы (новый rank_bases):", [b.name for b in D.rank_bases(primary, None)])

cands = D.generate_candidates(bases, budget=120, seed=12345)
print("кандидатов:", len(cands))
basekeys = {tuple(b.args) for b in bases}
print("копий базы:", sum(1 for c in cands if tuple(c.args) in basekeys),
      "| дублей:", len(cands) - len({tuple(c.args) for c in cands}))

KEYS = dict(D._KEYMAP)
KEYS["urepeats"] = "--dpi-desync-repeats"
mix = collections.Counter()
for c in cands:
    b = next(x for x in bases if c.source_bat.endswith(x.name + ")"))
    n = 0
    for knob, key in KEYS.items():
        va = [t for t in c.args if t.startswith(key + "=")]
        vb = [t for t in b.args if t.startswith(key + "=")]
        if va != vb:
            n += 1
    mix[n] += 1
print("число изменённых ручек -> сколько кандидатов:", dict(sorted(mix.items())))

print("первые 12 кандидатов:")
for c in cands[:12]:
    bname = c.source_bat.split("base=")[1].rstrip(")")
    base = next(x for x in bases if x.name == bname)
    diff = []
    for knob, key in KEYS.items():
        va = [t for t in c.args if t.startswith(key + "=")]
        vb = [t for t in base.args if t.startswith(key + "=")]
        if va != vb:
            diff.append(knob)
    print(f"  {c.name:12} база={bname:14} изменено: {', '.join(diff)}")
