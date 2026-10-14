"""Свои сайты в обход: нормализация ввода, парсинг списка, чтение/запись hostlist."""
import tempfile
import unittest
from pathlib import Path

from freeconnect import sites


class TestNormalize(unittest.TestCase):
    def test_plain_domain(self):
        self.assertEqual(sites.normalize("example.com"), "example.com")

    def test_strips_scheme_path_www_port(self):
        self.assertEqual(sites.normalize("https://www.Example.com:443/path?q=1"), "example.com")

    def test_keeps_subdomain(self):
        self.assertEqual(sites.normalize("cdn.site.co.uk"), "cdn.site.co.uk")

    def test_cyrillic_to_punycode(self):
        d = sites.normalize("почта.рф")
        self.assertTrue(d and d.startswith("xn--"))

    def test_rejects_garbage(self):
        for bad in ("", "   ", "# comment", "not a domain", "localhost", "http://", "..", "a_b.com"):
            self.assertIsNone(sites.normalize(bad), bad)


class TestParse(unittest.TestCase):
    def test_splits_dedups_preserves_order(self):
        text = "b.com\na.com, b.com\nhttps://a.com/x"
        self.assertEqual(sites.parse(text), ["b.com", "a.com"])

    def test_skips_comments_and_placeholder(self):
        text = f"# hi\n{sites.PLACEHOLDER}\nreal.com"
        self.assertEqual(sites.parse(text), ["real.com"])

    def test_empty(self):
        self.assertEqual(sites.parse(""), [])


class TestReadWrite(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = sites.paths.LISTS_DIR
        sites.paths.LISTS_DIR = Path(self._tmp.name)

    def tearDown(self):
        sites.paths.LISTS_DIR = self._orig
        self._tmp.cleanup()

    def test_roundtrip(self):
        n = sites.write_domains(["one.com", "two.org"])
        self.assertEqual(n, 2)
        self.assertEqual(sites.read_domains(), ["one.com", "two.org"])

    def test_empty_writes_placeholder_not_empty_file(self):
        sites.write_domains([])
        p = sites.paths.LISTS_DIR / sites.USER_LIST
        body = p.read_text(encoding="utf-8")
        self.assertIn(sites.PLACEHOLDER, body)      # winws не получит пустой hostlist
        self.assertEqual(sites.read_domains(), [])   # но реальных доменов нет

    def test_read_missing_file(self):
        self.assertEqual(sites.read_domains(), [])

    def test_merge_defaults_seeds_popular(self):
        n = sites.merge_defaults()
        self.assertEqual(n, len(sites.DEFAULT_DOMAINS))
        got = sites.read_domains()
        self.assertIn("pornhub.com", got)
        self.assertIn("phncdn.com", got)      # CDN тоже, не только apex
        self.assertIn("rutracker.org", got)

    def test_merge_defaults_keeps_user_domains(self):
        sites.write_domains(["my.custom.site"])
        sites.merge_defaults()
        got = sites.read_domains()
        self.assertIn("my.custom.site", got)   # пользовательский сохранён
        self.assertIn("pornhub.com", got)      # дефолт добавлен
        # без дублей и повторный вызов идемпотентен
        n1 = len(got)
        sites.merge_defaults()
        self.assertEqual(len(sites.read_domains()), n1)

    def test_defaults_are_valid_domains(self):
        for d in sites.DEFAULT_DOMAINS:
            self.assertEqual(sites.normalize(d), d, d)   # все нормализуются в себя

    def test_retired_pruned_from_existing_list(self):
        # У пользователя в списке лежит ретайрнутый порно-домен (из старого засева) и
        # свой домен. merge_defaults вычищает первый, сохраняет второй.
        retired = sites.RETIRED_DOMAINS[0]
        sites.write_domains([retired, "my.custom.site"])
        sites.merge_defaults()
        got = sites.read_domains()
        self.assertNotIn(retired, got)            # ретайрнутый убран
        self.assertIn("my.custom.site", got)      # пользовательский цел
        self.assertIn("pornhub.com", got)         # актуальный дефолт на месте

    def test_defaults_and_retired_disjoint(self):
        # Дефолт не должен одновременно быть в списке на вычистку.
        self.assertEqual(set(sites.DEFAULT_DOMAINS) & set(sites.RETIRED_DOMAINS), set())


if __name__ == "__main__":
    unittest.main()
