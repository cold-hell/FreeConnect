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
        self._orig_runtime = singbox.paths.RUNTIME_DIR
        self._orig_ensure = singbox.paths.ensure_dirs
        singbox.paths.RUNTIME_DIR = self.tmp
        singbox.paths.ensure_dirs = lambda: None   # не трогаем реальные C:\FreeConnect

    def tearDown(self):
        singbox.paths.RUNTIME_DIR = self._orig_runtime
        singbox.paths.ensure_dirs = self._orig_ensure

    def test_write_config_roundtrip(self):
        sb = singbox.SingBox()
        cfg = {"outbounds": [{"type": "hysteria2", "tag": "vpn"}], "route": {"final": "direct"}}
        sb.write_config(cfg)
        back = json.loads((self.tmp / "singbox.json").read_text(encoding="utf-8"))
        self.assertEqual(back, cfg)

    def test_cmd_has_run_and_config(self):
        sb = singbox.SingBox()
        cmd = sb._cmd()
        self.assertIn("run", cmd)
        self.assertIn("-c", cmd)
        self.assertEqual(cmd[-1], str(self.tmp / "singbox.json"))

    def test_named_instances_use_separate_files(self):
        """Пробник и основной туннель не должны затирать конфиг/лог друг друга."""
        main, probe = singbox.SingBox(), singbox.SingBox(name="singbox-probe")
        main.write_config({"a": 1})
        probe.write_config({"b": 2})
        self.assertNotEqual(main._cmd()[-1], probe._cmd()[-1])
        self.assertEqual(json.loads((self.tmp / "singbox.json").read_text("utf-8")), {"a": 1})
        self.assertEqual(json.loads((self.tmp / "singbox-probe.json").read_text("utf-8")), {"b": 2})

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
