# -*- coding: utf-8 -*-
"""Проверка починки «фейков»: битые файлы в готовом рантайме заменяются из бандла.

Сценарий ровно как у пользователя после обновления: в C:\\FreeConnect\\runtime\\bin
лежат файлы-заглушки (копия файла Discord под именем игрового, QUIC под именем STUN).
Приложение при старте должно их увидеть и перезаписать настоящими из бандла.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUNDLE_BIN = ROOT / "dist" / "FreeConnect" / "_internal" / "runtime" / "bin"
if not BUNDLE_BIN.is_dir():
    BUNDLE_BIN = Path(r"C:\FreeConnect\runtime\bin")  # до сборки — рабочий рантайм

home = Path(tempfile.mkdtemp(prefix="fc-heal-"))
os.environ["FREECONNECT_HOME"] = str(home)
sys.path.insert(0, str(ROOT))
for m in [m for m in list(sys.modules) if m.startswith("freeconnect")]:
    del sys.modules[m]

from freeconnect import app, paths  # noqa: E402
from freeconnect.strategy_update import _valid_payload  # noqa: E402

rt_bin = paths.BIN_DIR
rt_bin.mkdir(parents=True, exist_ok=True)

print(f"HOME={home}")
print(f"бандл: {BUNDLE_BIN}")

# 1) кладём «как было»: ACTIVE_GAME_UDP = копия Discord, stun2 = QUIC
discord_blob = (BUNDLE_BIN / "ACTIVE_DISCORD_UDP.bin").read_bytes()
quic_blob = (BUNDLE_BIN / "quic_initial_www_google_com.bin").read_bytes()
(rt_bin / "ACTIVE_GAME_UDP.bin").write_bytes(discord_blob)
(rt_bin / "stun2.bin").write_bytes(quic_blob)
print("\nдо починки:")
for n in ("ACTIVE_GAME_UDP.bin", "stun2.bin"):
    d = (rt_bin / n).read_bytes()
    print(f"  {n:24} {len(d):5}б валиден={_valid_payload(n, d)} md5={hashlib.md5(d).hexdigest()[:10]}")

# 2) запускаем починку (как при старте приложения)
app._heal_payloads(BUNDLE_BIN.parent)

print("\nпосле починки:")
ok = True
for n in ("ACTIVE_GAME_UDP.bin", "stun2.bin"):
    d = (rt_bin / n).read_bytes()
    valid = _valid_payload(n, d)
    ok = ok and valid
    print(f"  {n:24} {len(d):5}б валиден={valid} md5={hashlib.md5(d).hexdigest()[:10]}")

same = (rt_bin / "ACTIVE_GAME_UDP.bin").read_bytes() == (BUNDLE_BIN / "ACTIVE_GAME_UDP.bin").read_bytes()
print(f"\nигровой фейк после починки совпадает с бандлом (а не с Discord): {same}")
print(f"итог: {'OK' if ok and same else 'ПРОБЛЕМА'}")

shutil.rmtree(home, ignore_errors=True)
