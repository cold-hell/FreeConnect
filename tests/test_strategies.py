"""Санитайзер аргументов и подстановка путей/игровых портов (strategies)."""
import tempfile
import unittest
from pathlib import Path

from freeconnect import strategies as S
from freeconnect.strategies import Strategy, _sanitize_args


class TestSanitize(unittest.TestCase):
    def test_drops_caret_and_percent(self):
        args = ["--ok=1", "--bad=^!", "--var=%UNRESOLVED%", "--good=2"]
        self.assertEqual(_sanitize_args(args), ["--ok=1", "--good=2"])

    def test_drops_zero_fake_tls(self):
        self.assertEqual(_sanitize_args(["--dpi-desync-fake-tls=0x00000000", "--x"]), ["--x"])

    def test_keeps_normal(self):
        args = ["--dpi-desync=fake,split2", "--dpi-desync-fooling=md5sig"]
        self.assertEqual(_sanitize_args(args), args)


class TestEnsureBlobs(unittest.TestCase):
    """winws падает, если стратегия ссылается на несуществующий .bin (апстрим перешёл
    на живой захват ACTIVE_*.bin) — ensure_referenced_blobs кладёт фолбэк-копии."""
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._home, self._bin, self._sj = S.paths.APP_HOME, S.paths.BIN_DIR, S.paths.STRATEGIES_JSON
        root = Path(self._tmp.name)
        S.paths.APP_HOME = root
        S.paths.BIN_DIR = root / "bin"
        S.paths.STRATEGIES_JSON = root / "strategies.json"
        S.paths.BIN_DIR.mkdir()
        # имеющиеся блобы
        for n in ("quic_initial_dbankcloud_ru.bin", "tls_clienthello_www_google_com.bin"):
            (S.paths.BIN_DIR / n).write_bytes(b"X")

    def tearDown(self):
        S.paths.APP_HOME, S.paths.BIN_DIR, S.paths.STRATEGIES_JSON = self._home, self._bin, self._sj
        self._tmp.cleanup()

    def test_creates_fallback_for_missing_blobs(self):
        S.paths.STRATEGIES_JSON.write_text(
            '{"strategies":[{"id":"a","name":"a","source_bat":"a","args":['
            '"--dpi-desync-fake-discord={BIN}/ACTIVE_DISCORD_UDP.bin",'
            '"--dpi-desync-fake-tls={BIN}/ACTIVE_TLS_MISSING.bin",'
            '"--dpi-desync-fake-quic={BIN}/quic_initial_dbankcloud_ru.bin"]}]}',
            encoding="utf-8")
        made = S.ensure_referenced_blobs()
        self.assertEqual(made, 2)                                   # два недостающих
        self.assertTrue((S.paths.BIN_DIR / "ACTIVE_DISCORD_UDP.bin").exists())
        self.assertTrue((S.paths.BIN_DIR / "ACTIVE_TLS_MISSING.bin").exists())
        # tls-имя берёт tls-фолбэк, прочее — discord/quic-фолбэк
        self.assertEqual((S.paths.BIN_DIR / "ACTIVE_TLS_MISSING.bin").read_bytes(),
                         (S.paths.BIN_DIR / "tls_clienthello_www_google_com.bin").read_bytes())

    def test_idempotent_and_noop_when_all_present(self):
        S.paths.STRATEGIES_JSON.write_text(
            '{"strategies":[{"id":"a","name":"a","source_bat":"a","args":['
            '"--x={BIN}/quic_initial_dbankcloud_ru.bin"]}]}', encoding="utf-8")
        self.assertEqual(S.ensure_referenced_blobs(), 0)


class TestResolveArgs(unittest.TestCase):
    def _mk(self, args):
        return Strategy(id="x", name="x", source_bat="x", args=args)

    def test_game_filter_on_off(self):
        st = self._mk(["--wf-tcp={GAME_TCP}", "--wf-udp={GAME_UDP}"])
        on = st.resolve_args(game_filter=True)
        off = st.resolve_args(game_filter=False)
        self.assertEqual(on, ["--wf-tcp=1024-65535", "--wf-udp=1024-65535"])
        self.assertEqual(off, ["--wf-tcp=12", "--wf-udp=12"])

    def test_path_placeholders_substituted(self):
        st = self._mk(["--hostlist={LISTS}/l.txt", "--fake={BIN}/f.bin"])
        out = st.resolve_args(game_filter=False)
        # плейсхолдеры должны исчезнуть, пути стать абсолютными (с прямыми слэшами)
        self.assertFalse(any("{" in a for a in out))
        self.assertTrue(out[0].endswith("/l.txt"))
        self.assertTrue(out[1].endswith("/f.bin"))
        self.assertIn("runtime", out[0].replace("\\", "/").lower())


if __name__ == "__main__":
    unittest.main()
