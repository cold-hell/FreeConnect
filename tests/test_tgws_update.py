"""Проверка свежести ядра Telegram (freeconnect/tgws_update.py).

В сеть не ходим: подменяем опенер, который возвращает заранее заданный ответ
GitHub Releases.
"""
import json
import time
import unittest
from unittest import mock

from freeconnect import tgws_update


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        if isinstance(self._payload, bytes):
            return self._payload
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Opener:
    """Опенер-заглушка: либо отдаёт ответ, либо падает заданной ошибкой."""

    def __init__(self, payload=None, error=None):
        self.payload, self.error = payload, error
        self.calls = 0

    def open(self, request, timeout=None):  # noqa: ARG002
        self.calls += 1
        if self.error is not None:
            raise self.error
        return _Response(self.payload)


def _patch_opener(opener):
    return mock.patch.object(tgws_update, "_opener", return_value=opener)


def _patch_local(version="1.11.2"):
    return mock.patch.object(tgws_update.tgproxy, "core_version", return_value=version)


class TestVersions(unittest.TestCase):
    def test_parse_version(self):
        self.assertEqual(tgws_update.parse_version("v1.11.2"), (1, 11, 2))
        self.assertEqual(tgws_update.parse_version("1.12"), (1, 12))
        self.assertEqual(tgws_update.parse_version(""), ())
        self.assertEqual(tgws_update.parse_version(None), ())

    def test_is_newer(self):
        self.assertTrue(tgws_update.is_newer("1.12.0", "1.11.2"))
        self.assertTrue(tgws_update.is_newer("v1.11.10", "1.11.9"))
        self.assertTrue(tgws_update.is_newer("1.11.2.1", "1.11.2"))
        self.assertFalse(tgws_update.is_newer("1.11.2", "1.11.2"))
        self.assertFalse(tgws_update.is_newer("1.11.1", "1.11.2"))
        self.assertFalse(tgws_update.is_newer("", "1.11.2"))
        self.assertFalse(tgws_update.is_newer("v", "1.11.2"))


class TestCheck(unittest.TestCase):
    def test_available_when_upstream_is_newer(self):
        opener = _Opener(payload={"tag_name": "v1.12.0",
                                  "html_url": "https://example/rel"})
        with _patch_opener(opener), _patch_local("1.11.2"):
            info = tgws_update.check()
        self.assertTrue(info["available"])
        self.assertEqual(info["latest"], "1.12.0")
        self.assertEqual(info["local"], "1.11.2")
        self.assertEqual(info["url"], "https://example/rel")
        self.assertEqual(info["error"], "")

    def test_not_available_when_same_version(self):
        opener = _Opener(payload={"tag_name": "v1.11.2"})
        with _patch_opener(opener), _patch_local("1.11.2"):
            info = tgws_update.check()
        self.assertFalse(info["available"])

    def test_network_error_is_reported_not_raised(self):
        opener = _Opener(error=OSError("нет сети"))
        with _patch_opener(opener), _patch_local("1.11.2"):
            info = tgws_update.check()
        self.assertFalse(info["available"])
        self.assertIn("нет сети", info["error"])

    def test_broken_json_is_reported(self):
        opener = _Opener(payload="{это не json".encode("utf-8"))
        with _patch_opener(opener), _patch_local("1.11.2"):
            info = tgws_update.check()
        self.assertFalse(info["available"])
        self.assertTrue(info["error"])


class TestMaybeCheck(unittest.TestCase):
    def test_skips_when_checked_recently(self):
        cfg = {"tg_core_checked_at": time.time(), "tg_core_latest": "1.12.0"}
        opener = _Opener(payload={"tag_name": "v1.13.0"})
        with _patch_opener(opener), _patch_local("1.11.2"):
            info = tgws_update.maybe_check(cfg)
        self.assertFalse(info["checked"])
        self.assertEqual(opener.calls, 0)                 # в сеть не пошли
        self.assertTrue(info["available"])                # но о новой версии помним

    def test_checks_when_stale_and_writes_config(self):
        cfg = {"tg_core_checked_at": 0}
        opener = _Opener(payload={"tag_name": "v1.12.0"})
        before = time.time()
        with _patch_opener(opener), _patch_local("1.11.2"):
            info = tgws_update.maybe_check(cfg)
        self.assertTrue(info["checked"])
        self.assertEqual(opener.calls, 1)
        self.assertEqual(cfg["tg_core_latest"], "1.12.0")
        self.assertGreaterEqual(cfg["tg_core_checked_at"], before)

    def test_failed_check_does_not_update_timestamp(self):
        cfg = {"tg_core_checked_at": 0}
        opener = _Opener(error=OSError("нет сети"))
        with _patch_opener(opener), _patch_local("1.11.2"):
            info = tgws_update.maybe_check(cfg)
        self.assertTrue(info["checked"])
        self.assertEqual(cfg.get("tg_core_checked_at", 0), 0)   # повторим позже
        self.assertNotIn("tg_core_latest", cfg)


if __name__ == "__main__":
    unittest.main()
