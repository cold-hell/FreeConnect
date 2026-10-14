"""Сравнение версий для автообновления приложения (app_update)."""
import io
import json
import unittest

from freeconnect import app_update as au


class TestParseVer(unittest.TestCase):
    def test_v_prefix_and_plain(self):
        self.assertEqual(au._parse_ver("v1.2.3"), (1, 2, 3))
        self.assertEqual(au._parse_ver("1.2"), (1, 2))

    def test_empty_and_garbage(self):
        self.assertEqual(au._parse_ver(""), (0,))
        self.assertEqual(au._parse_ver("beta"), (0,))


class TestIsNewer(unittest.TestCase):
    def test_newer(self):
        self.assertTrue(au._is_newer("v0.1.1", "0.1.0"))
        self.assertTrue(au._is_newer("v1.0", "0.9"))
        self.assertTrue(au._is_newer("1.0.1", "1.0"))  # разная длина

    def test_equal_or_older(self):
        self.assertFalse(au._is_newer("0.1.0", "0.1.0"))
        self.assertFalse(au._is_newer("1.0", "1.0.0"))  # равны после выравнивания
        self.assertFalse(au._is_newer("0.1.0", "v0.1.1"))


class TestCheckGuards(unittest.TestCase):
    def test_repo_configured(self):
        # На случай, если кто-то забудет заполнить константу перед сборкой.
        self.assertNotIn("__", au.GITHUB_REPO)
        self.assertIn("/", au.GITHUB_REPO)


def _zip_with_latest(payload: dict) -> bytes:
    """codeload отдаёт zip, где файлы завёрнуты в подпапку <org>-<repo>-<hash>/."""
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("millerloves-freeconnect-mirror-abc123/README.md", "mirror")
        z.writestr("millerloves-freeconnect-mirror-abc123/latest.json",
                   json.dumps(payload))
    return buf.getvalue()


class TestReadLatestFromZip(unittest.TestCase):
    def test_finds_nested_latest_json(self):
        data = _zip_with_latest({"version": "v0.1.10"})
        meta = au._read_latest_json_from_zip(data)
        self.assertEqual(meta["version"], "v0.1.10")


class TestMirrorFallback(unittest.TestCase):
    """Когда GitHub у пользователя недоступен — данные берём с зеркала (SourceCraft)."""

    def setUp(self):
        self._reach = au.github_reachable
        self._open = au.urllib.request.urlopen
        self._mirror = au.MIRROR_LATEST
        self._ver = au.__version__

    def tearDown(self):
        au.github_reachable = self._reach
        au.urllib.request.urlopen = self._open
        au.MIRROR_LATEST = self._mirror
        au.__version__ = self._ver

    def _fake_mirror_response(self, payload):
        class _Resp(io.BytesIO):
            def __enter__(self_):
                return self_
            def __exit__(self_, *a):
                self_.close()
        blob = _zip_with_latest(payload)
        def _open(req, timeout=0):
            url = req.full_url if hasattr(req, "full_url") else req
            if url == au.MIRROR_LATEST:
                return _Resp(blob)
            raise AssertionError(f"неожиданный запрос: {url}")
        return _open

    def test_uses_mirror_when_github_down(self):
        au.github_reachable = lambda timeout=4.0: False   # GitHub недоступен
        au.MIRROR_LATEST = "https://codeload.example/zipball/refs/heads/main"
        au.__version__ = "0.1.9"
        au.urllib.request.urlopen = self._fake_mirror_response(
            {"version": "v0.1.10",
             "zipball": "https://codeload.example/zipball/refs/heads/dist",
             "exe": "FreeConnect-Setup.exe", "notes": "fix"})
        out = au.check(timeout=1.0)
        self.assertEqual(out["source"], "mirror")
        self.assertTrue(out["available"])
        self.assertEqual(out["version"], "v0.1.10")
        self.assertIn("/zipball/", out["url"])
        self.assertEqual(out["exe"], "FreeConnect-Setup.exe")

    def test_falls_back_to_mirror_when_github_answers_but_has_no_release(self):
        """GitHub открывается, но релиза нет (репозиторий переименован/переехал, сбой
        API). Раньше это молча гасило обновления — теперь спрашиваем зеркало, оно от
        имени аккаунта на GitHub не зависит."""
        au.github_reachable = lambda timeout=4.0: True     # GitHub доступен...
        au.MIRROR_LATEST = "https://codeload.example/zipball/refs/heads/main"
        au.__version__ = "0.1.9"
        blob = _zip_with_latest(
            {"version": "v0.1.10",
             "zipball": "https://codeload.example/zipball/refs/heads/dist",
             "exe": "FreeConnect-Setup.exe", "notes": ""})

        class _Resp(io.BytesIO):
            def __enter__(self_): return self_
            def __exit__(self_, *a): self_.close()

        def _open(req, timeout=0):
            url = req.full_url if hasattr(req, "full_url") else req
            if url == au.MIRROR_LATEST:
                return _Resp(blob)
            raise OSError("404 — репозиторий по старому имени не найден")

        au.urllib.request.urlopen = _open
        out = au.check(timeout=1.0)
        self.assertEqual(out["source"], "mirror")   # подстраховались зеркалом
        self.assertTrue(out["available"])
        self.assertEqual(out["version"], "v0.1.10")

    def test_mirror_not_configured_reports_error(self):
        au.github_reachable = lambda timeout=4.0: False
        au.MIRROR_LATEST = ""
        out = au.check(timeout=1.0)
        self.assertEqual(out["source"], "mirror")
        self.assertFalse(out["available"])
        self.assertIn("зеркало не настроено", out["error"])

    def test_mirror_same_version_not_available(self):
        au.github_reachable = lambda timeout=4.0: False
        au.MIRROR_LATEST = "https://codeload.example/zipball/refs/heads/main"
        au.__version__ = "0.1.10"
        au.urllib.request.urlopen = self._fake_mirror_response(
            {"version": "v0.1.10", "zipball": "https://codeload.example/x"})
        out = au.check(timeout=1.0)
        self.assertFalse(out["available"])   # уже на последней — баннера нет


if __name__ == "__main__":
    unittest.main()
