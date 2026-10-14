"""Парсер .bat -> аргументы winws (strategy_update). Критично: результат кормит
winws, битый токен роняет обход. Проверяем склейку строк, извлечение, токенизацию,
нормализацию плейсхолдеров и отсев артефактов."""
import unittest

from freeconnect import strategy_update as su


class TestJoinContinuations(unittest.TestCase):
    def test_caret_newline_joined(self):
        # Каретка-продолжение снимается; лишние пробелы схлопнет токенизатор.
        for src in ("a ^\r\nb", "a ^\nb"):
            joined = su._join_continuations(src)
            self.assertNotIn("^", joined)
            self.assertEqual(su.tokenize(joined), ["a", "b"])

    def test_mid_line_caret_kept(self):
        # ^! не в конце строки — это артефакт batch, склейка его НЕ трогает.
        self.assertIn("^!", su._join_continuations("x=^! y"))


class TestExtract(unittest.TestCase):
    def test_extract_after_winws(self):
        line = 'start "z" /min "%~dp0bin\\winws.exe" --wf-tcp=80 --new'
        self.assertEqual(su.extract_winws_args(line), "--wf-tcp=80 --new")

    def test_no_winws_returns_none(self):
        self.assertIsNone(su.extract_winws_args("echo hello"))


class TestTokenize(unittest.TestCase):
    def test_quotes_stripped_and_spaces_split(self):
        toks = su.tokenize('--a=1 --host="c:\\path with space\\l.txt" --b')
        self.assertEqual(toks, ["--a=1", "--host=c:\\path with space\\l.txt", "--b"])


class TestNormalize(unittest.TestCase):
    def test_bin_lists_placeholders(self):
        self.assertEqual(su.normalize_token("%~dp0bin\\winws.exe"), "{BIN}/winws.exe")
        self.assertEqual(su.normalize_token("%~dp0lists\\l.txt"), "{LISTS}/l.txt")
        self.assertEqual(su.normalize_token("%BIN%fake.bin"), "{BIN}/fake.bin")

    def test_game_filter_placeholder(self):
        self.assertEqual(su.normalize_token("%GameFilterTCP%"), "{GAME_TCP}")
        self.assertEqual(su.normalize_token("%GameFilterUDP%"), "{GAME_UDP}")

    def test_backslash_and_double_slash_collapsed(self):
        self.assertEqual(su.normalize_token("%~dp0bin\\sub\\f.bin"), "{BIN}/sub/f.bin")


class TestFriendlyName(unittest.TestCase):
    def test_general_variants(self):
        self.assertEqual(su.friendly_name("general (ALT).bat"), "ALT")
        self.assertEqual(su.friendly_name("general.bat"), "Default")
        self.assertEqual(su.friendly_name("general (FAKE TLS AUTO).bat"), "FAKE TLS AUTO")


SAMPLE_BAT = (
    "@echo off\r\n"
    'start "zapret: general" /min "%~dp0bin\\winws.exe" ^\r\n'
    "--wf-tcp=80,443 ^\r\n"
    '--filter-tcp=443 --hostlist="%~dp0lists\\list-general.txt" ^\r\n'
    "--dpi-desync=fake,split2 --dpi-desync-fake-tls=^! "
    "--dpi-desync-fooling=md5sig %GameFilterTCP%\r\n"
)


class TestParseBatText(unittest.TestCase):
    def setUp(self):
        self.s = su.parse_bat_text(SAMPLE_BAT, "general (TEST).bat")

    def test_id_and_name(self):
        self.assertEqual(self.s["id"], "general (TEST)")
        self.assertEqual(self.s["name"], "TEST")

    def test_expected_tokens_present(self):
        args = self.s["args"]
        self.assertIn("--wf-tcp=80,443", args)
        self.assertIn("--hostlist={LISTS}/list-general.txt", args)
        self.assertIn("--dpi-desync=fake,split2", args)
        self.assertIn("{GAME_TCP}", args)

    def test_caret_artifact_sanitized(self):
        # --dpi-desync-fake-tls=^! содержит ^ -> должен быть выброшен санитайзером.
        self.assertFalse(any("^" in a for a in self.s["args"]))
        self.assertNotIn("--dpi-desync-fake-tls=^!", self.s["args"])

    def test_no_winws_returns_none(self):
        self.assertIsNone(su.parse_bat_text("echo nothing here", "x.bat"))


if __name__ == "__main__":
    unittest.main()
