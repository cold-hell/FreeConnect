# -*- coding: utf-8 -*-
"""Тесты «фейков» (пакетов-обманок) и их починки.

Разбор жалобы: игровой фильтр не работал на европейских серверах FACEIT, потому
что в комплекте лежали битые заглушки — ACTIVE_GAME_UDP.bin был копией файла
Discord, stun2.bin — QUIC-пакетом вместо STUN. winws запускался и «работал», но
обманка не выглядела как настоящий протокол, поэтому DPI её не обманывала.
"""
from __future__ import annotations

import os
import shutil
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from freeconnect import strategy_update as SU

ROOT = Path(__file__).resolve().parents[1]


def _quic(size: int = 1200) -> bytes:
    """Минимальный «QUIC initial»: long header + version 1."""
    return bytes([0xC0]) + struct.pack("!I", 1) + b"\x00" * (size - 5)


def _stun(size: int = 120) -> bytes:
    return b"\x00\x01" + struct.pack("!H", size - 20) + b"\x21\x12\xa4\x42" + b"\x00" * (size - 8)


def _tls(size: int = 681) -> bytes:
    return b"\x16\x03\x01" + b"\x00" * (size - 3)


class TestPayloadValidation(unittest.TestCase):
    """Валидатор должен отличать настоящий пакет от заглушки."""

    def test_quic_payloads(self):
        self.assertTrue(SU._valid_payload("ACTIVE_GAME_UDP.bin", _quic()))
        self.assertTrue(SU._valid_payload("quic_initial_steamcommunity_com.bin", _quic()))
        self.assertFalse(SU._valid_payload("ACTIVE_GAME_UDP.bin", _stun()))

    def test_stun_payloads(self):
        self.assertTrue(SU._valid_payload("stun.bin", _stun()))
        self.assertTrue(SU._valid_payload("stun2.bin", _stun()))
        # именно этот случай ломал игровой обход: QUIC под именем STUN
        self.assertFalse(SU._valid_payload("stun2.bin", _quic()))

    def test_tls_payloads(self):
        self.assertTrue(SU._valid_payload("tls_clienthello_www_google_com.bin", _tls()))
        self.assertFalse(SU._valid_payload("tls_clienthello_max_ru.bin", _quic()))

    def test_garbage_rejected(self):
        self.assertFalse(SU._valid_payload("ACTIVE_GAME_UDP.bin", b""))
        self.assertFalse(SU._valid_payload("stun.bin", b"\x00" * 100))


class TestSyncFromUpstream(unittest.TestCase):
    """Синхронизация качает фейки из апстрима и не портит файл битым ответом."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="fc-sync-"))
        self._env = os.environ.get("FREECONNECT_HOME")
        os.environ["FREECONNECT_HOME"] = str(self.home)
        import importlib
        import freeconnect.paths as paths_mod
        importlib.reload(paths_mod)
        self.paths = paths_mod
        self.paths.BIN_DIR.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        if self._env is None:
            os.environ.pop("FREECONNECT_HOME", None)
        else:
            os.environ["FREECONNECT_HOME"] = self._env
        shutil.rmtree(self.home, ignore_errors=True)

    def test_writes_valid_payload(self):
        with mock.patch.object(SU, "_UA", {}), \
             mock.patch("freeconnect.strategies._referenced_blobs", return_value={"stun2.bin"}), \
             mock.patch.object(SU.urllib.request, "urlopen") as up:
            up.return_value.__enter__.return_value.read.return_value = _stun()
            made = SU.sync_fake_payloads()
        self.assertEqual(made, 1)
        self.assertTrue(SU._valid_payload("stun2.bin", (self.paths.BIN_DIR / "stun2.bin").read_bytes()))

    def test_does_not_overwrite_with_garbage(self):
        target = self.paths.BIN_DIR / "stun2.bin"
        target.write_bytes(_stun())
        with mock.patch.object(SU, "_UA", {}), \
             mock.patch("freeconnect.strategies._referenced_blobs", return_value={"stun2.bin"}), \
             mock.patch.object(SU.urllib.request, "urlopen") as up:
            up.return_value.__enter__.return_value.read.return_value = _quic()
            made = SU.sync_fake_payloads()
        self.assertEqual(made, 0, "битый ответ не должен затирать рабочий фейк")
        self.assertTrue(SU._valid_payload("stun2.bin", target.read_bytes()))


if __name__ == "__main__":
    unittest.main()
