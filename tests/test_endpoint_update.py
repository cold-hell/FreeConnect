"""Доставка живых адресов Telegram через наш канал обновлений: разбор публикуемого
файла, слияние с локальными находками, фолбэк GitHub -> зеркало. Сеть не трогаем."""
import io
import json
import unittest
import zipfile

from freeconnect import endpoint_update as eu


def _zip_with(path: str, payload: dict) -> bytes:
    """codeload-архив: файлы лежат в подпапке <org>-<repo>-<hash>/."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(f"org-repo-abc123/{path}", json.dumps(payload))
    return buf.getvalue()


class TestValidation(unittest.TestCase):
    def test_keeps_only_valid_ipv4(self):
        got = eu._valid({"endpoints": {"2": ["149.154.167.220", "не-ip", "999.1.1.1"]}})
        self.assertEqual(got, {"2": ["149.154.167.220"]})

    def test_normalizes_dc_key_to_string(self):
        self.assertEqual(eu._valid({"endpoints": {2: ["1.2.3.4"]}}), {"2": ["1.2.3.4"]})

    def test_drops_garbage(self):
        for bad in (None, [], "строка", {"endpoints": None},
                    {"endpoints": {"нет-номера": ["1.2.3.4"]}},
                    {"endpoints": {"2": "не-список"}}):
            self.assertEqual(eu._valid(bad), {})

    def test_caps_list_length(self):
        many = [f"1.2.3.{i}" for i in range(1, 12)]
        self.assertEqual(len(eu._valid({"endpoints": {"2": many}})["2"]), eu.MAX_PER_DC)


class TestValidCf(unittest.TestCase):
    def test_keeps_valid_domains(self):
        got = eu._valid_cf({"cf_domains": ["Relay.Example.Com", "bad domain", "ok.net"]})
        self.assertEqual(got, ["relay.example.com", "ok.net"])   # нормализует регистр, чистит мусор

    def test_drops_garbage(self):
        for bad in (None, [], "строка", {"cf_domains": None},
                    {"cf_domains": ["nodot", "", 123]}):
            self.assertEqual(eu._valid_cf(bad), [])

    def test_caps_length(self):
        many = [f"d{i}.example.com" for i in range(20)]
        self.assertEqual(len(eu._valid_cf({"cf_domains": many})), eu.MAX_CF_DOMAINS)


class TestMerge(unittest.TestCase):
    def test_remote_first_local_kept(self):
        # Локально найденный адрес терять нельзя: у этого провайдера может работать он.
        got = eu.merge({"2": ["10.0.0.9"]}, {"2": ["149.154.167.220"]})
        self.assertEqual(got["2"], ["149.154.167.220", "10.0.0.9"])

    def test_no_duplicates(self):
        got = eu.merge({"2": ["1.2.3.4"]}, {"2": ["1.2.3.4"]})
        self.assertEqual(got["2"], ["1.2.3.4"])

    def test_untouched_dc_survives(self):
        got = eu.merge({"5": ["10.0.0.5"]}, {"2": ["1.2.3.4"]})
        self.assertEqual(got["5"], ["10.0.0.5"])
        self.assertEqual(got["2"], ["1.2.3.4"])

    def test_merge_respects_cap(self):
        local = [f"10.0.0.{i}" for i in range(1, 6)]
        got = eu.merge({"2": local}, {"2": ["1.2.3.4"]})
        self.assertEqual(len(got["2"]), eu.MAX_PER_DC)
        self.assertEqual(got["2"][0], "1.2.3.4")     # свежий — первым


class TestFetch(unittest.TestCase):
    PAYLOAD = {"endpoints": {"2": ["149.154.167.220"]}}

    def setUp(self):
        self._reach = eu.github_reachable
        self._open = eu.urllib.request.urlopen

    def tearDown(self):
        eu.github_reachable = self._reach
        eu.urllib.request.urlopen = self._open

    def _patch(self, reachable, github=None, mirror=None):
        eu.github_reachable = lambda timeout=4.0: reachable

        class _R(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): self.close()

        def fake(req, timeout=0):
            url = req.full_url
            if url == eu.GITHUB_RAW:
                if github is None:
                    raise OSError("github режется")
                return _R(json.dumps(github).encode())
            if mirror is None:
                raise OSError("зеркало недоступно")
            return _R(mirror)

        eu.urllib.request.urlopen = fake

    def test_github_preferred(self):
        self._patch(True, github=self.PAYLOAD)
        eps, err = eu.fetch_remote()
        self.assertEqual(eps, {"2": ["149.154.167.220"]})
        self.assertEqual(err, "")

    def test_falls_back_to_mirror_when_github_blocked(self):
        # Провайдер режет GitHub — адреса должны приехать с зеркала.
        self._patch(False, mirror=_zip_with(eu.ENDPOINTS_PATH, self.PAYLOAD))
        eps, err = eu.fetch_remote()
        self.assertEqual(eps, {"2": ["149.154.167.220"]})
        self.assertEqual(err, "")

    def test_falls_back_when_github_errors(self):
        self._patch(True, github=None,
                    mirror=_zip_with(eu.ENDPOINTS_PATH, self.PAYLOAD))
        eps, err = eu.fetch_remote()
        self.assertEqual(eps, {"2": ["149.154.167.220"]})

    def test_both_channels_down(self):
        self._patch(False)
        eps, err = eu.fetch_remote()
        self.assertEqual(eps, {})
        self.assertTrue(err)

    def test_fetch_cf_domains_from_github(self):
        self._patch(True, github={"cf_domains": ["relay.example.com"]})
        doms, err = eu.fetch_cf_domains()
        self.assertEqual(doms, ["relay.example.com"])
        self.assertEqual(err, "")

    def test_fetch_cf_domains_from_mirror(self):
        self._patch(False, mirror=_zip_with(eu.ENDPOINTS_PATH,
                                            {"cf_domains": ["relay.example.com"]}))
        doms, err = eu.fetch_cf_domains()
        self.assertEqual(doms, ["relay.example.com"])


class TestMaybeUpdate(unittest.TestCase):
    def _run(self, cfg, remote, err=""):
        from freeconnect import config
        saved = {}
        orig_load, orig_save = config.load, config.save
        orig_fetch = eu.fetch_remote
        config.load = lambda: dict(cfg)
        config.save = lambda c: saved.update(c)
        eu.fetch_remote = lambda timeout=10.0: (remote, err)
        try:
            return eu.maybe_update(), saved
        finally:
            config.load, config.save = orig_load, orig_save
            eu.fetch_remote = orig_fetch

    def test_skips_when_recently_checked(self):
        import time
        (merged, why), saved = self._run(
            {"tg_endpoints_updated_at": time.time()}, {"2": ["1.2.3.4"]})
        self.assertEqual(merged, {})
        self.assertIn("недавно", why)
        self.assertEqual(saved, {})       # сеть не дёргали, конфиг не трогали

    def test_applies_and_persists(self):
        (merged, why), saved = self._run({}, {"2": ["149.154.167.220"]})
        self.assertEqual(merged["2"], ["149.154.167.220"])
        self.assertEqual(saved["tg_endpoints"], merged)
        self.assertTrue(saved["tg_endpoints_updated_at"])

    def test_no_change_still_records_check_time(self):
        cfg = {"tg_endpoints": {"2": ["1.2.3.4"]}}
        (merged, why), saved = self._run(cfg, {"2": ["1.2.3.4"]})
        self.assertEqual(merged, {})      # менять нечего
        self.assertTrue(saved["tg_endpoints_updated_at"])

    def test_channel_failure_is_not_fatal(self):
        (merged, why), saved = self._run({}, {}, err="зеркало недоступно")
        self.assertEqual(merged, {})
        self.assertIn("зеркало", why)


class TestMaybeUpdateCf(unittest.TestCase):
    def _run(self, cfg, remote, err=""):
        from freeconnect import config
        saved = {}
        orig_load, orig_save = config.load, config.save
        orig_fetch = eu.fetch_cf_domains
        config.load = lambda: dict(cfg)
        config.save = lambda c: saved.update(c)
        eu.fetch_cf_domains = lambda timeout=10.0: (remote, err)
        try:
            return eu.maybe_update_cf_domains(), saved
        finally:
            config.load, config.save = orig_load, orig_save
            eu.fetch_cf_domains = orig_fetch

    def test_skips_when_recently_checked(self):
        import time
        (doms, why), saved = self._run(
            {"tg_cf_updated_at": time.time()}, ["relay.example.com"])
        self.assertEqual(doms, [])
        self.assertIn("недавно", why)
        self.assertEqual(saved, {})

    def test_applies_and_persists(self):
        (doms, why), saved = self._run({}, ["relay.example.com"])
        self.assertEqual(doms, ["relay.example.com"])
        self.assertEqual(saved["tg_cf_domains"], ["relay.example.com"])
        self.assertTrue(saved["tg_cf_updated_at"])

    def test_no_change_records_check_time(self):
        cfg = {"tg_cf_domains": ["relay.example.com"]}
        (doms, why), saved = self._run(cfg, ["relay.example.com"])
        self.assertEqual(doms, [])
        self.assertTrue(saved["tg_cf_updated_at"])

    def test_channel_failure_not_fatal(self):
        (doms, why), saved = self._run({}, [], err="зеркало недоступно")
        self.assertEqual(doms, [])
        self.assertTrue(saved["tg_cf_updated_at"])   # время проверки записали


if __name__ == "__main__":
    unittest.main()
