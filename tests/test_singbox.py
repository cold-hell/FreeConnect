"""Тестируемые без бинарника части движка sing-box: запись конфига, команда,
флаг доступности. Реальный запуск TUN/процесса проверяется на машине пользователя."""
import json
import pathlib
import tempfile
import unittest

from freeconnect import singbox


class TestSingBox(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self._orig_cfg = singbox.SINGBOX_CONFIG
        self._orig_ensure = singbox.paths.ensure_dirs
        singbox.SINGBOX_CONFIG = self.tmp / "singbox.json"
        singbox.paths.ensure_dirs = lambda: None   # не трогаем реальные C:\FreeConnect

    def tearDown(self):
        singbox.SINGBOX_CONFIG = self._orig_cfg
        singbox.paths.ensure_dirs = self._orig_ensure

    def test_write_config_roundtrip(self):
        sb = singbox.SingBox()
        cfg = {"outbounds": [{"type": "hysteria2", "tag": "vpn"}], "route": {"final": "direct"}}
        sb.write_config(cfg)
        back = json.loads(singbox.SINGBOX_CONFIG.read_text(encoding="utf-8"))
        self.assertEqual(back, cfg)

    def test_cmd_has_run_and_config(self):
        sb = singbox.SingBox()
        cmd = sb._cmd()
        self.assertIn("run", cmd)
        self.assertIn("-c", cmd)
        self.assertEqual(cmd[-1], str(singbox.SINGBOX_CONFIG))

    def test_available_reflects_binary(self):
        sb = singbox.SingBox()
        # В тест-окружении бинарника нет — фича недоступна, но не падает.
        self.assertIsInstance(sb.available(), bool)

    def test_stop_is_safe_without_process(self):
        sb = singbox.SingBox()
        sb.stop()   # не должно бросать, даже если ничего не запущено
        self.assertFalse(sb.is_running())


if __name__ == "__main__":
    unittest.main()
