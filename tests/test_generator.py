# -*- coding: utf-8 -*-
"""Тесты генератора своих стратегий: состав кандидатов, базы, досрочная остановка.

Проверяем именно те дефекты, из-за которых «генерируется и ничего не создаёт»:
  * кандидат-копия базы (мутация «в себя») больше не попадает в перебор;
  * ручки выставляются там, где они вообще что-то меняют;
  * базы выбираются по прошлым результатам пользователя, а не жёстким ALT;
  * перебор останавливается, если базы явно не подходят провайдеру;
  * голосовой фейк Discord не подменяется блобом другого протокола.
"""
from __future__ import annotations

import unittest
from unittest import mock

from freeconnect import deepsearch as D
from freeconnect.autosearch import StrategyScore
from freeconnect.strategies import Strategy
from freeconnect.tester import ServiceResult, SiteResult


def _score(strategy: Strategy, site_ok: bool, voice_ok: bool) -> StrategyScore:
    """StrategyScore с одним сервисом Discord и заданными вердиктами."""
    site = SiteResult(host="discord.com", ok=site_ok, status="OK" if site_ok else "TIMEOUT",
                      latency_ms=100.0 if site_ok else -1.0)
    disc = ServiceResult(service="discord", sites=[site], voice_ok=voice_ok,
                         voice_conf="high" if voice_ok else "")
    yt = SiteResult(host="www.youtube.com", ok=site_ok, status="OK" if site_ok else "TIMEOUT")
    yt_svc = ServiceResult(service="youtube", sites=[yt], voice_ok=None)
    sc = StrategyScore(strategy=strategy, services=[disc, yt_svc], score=0.0)
    sc.working = disc.ok
    return sc

try:
    from freeconnect import paths
    from freeconnect.strategies import load_strategies
    _HAS_RUNTIME = paths.STRATEGIES_JSON.is_file()
except Exception:  # noqa: BLE001
    _HAS_RUNTIME = False

requires_runtime = unittest.skipUnless(
    _HAS_RUNTIME, "нет runtime/strategies.json (набор стратегий не загружен)")


def _knob_key(knob: str) -> str:
    """Ключ winws для ручки генератора (как в коде)."""
    if knob in D._KEYMAP:
        return D._KEYMAP[knob]
    if knob == "urepeats":
        return "--dpi-desync-repeats"
    if knob == "ufake":
        return D._UDP_FAKE_KEYS[0]
    raise AssertionError(f"неизвестная ручка {knob}")


class TestGenerateCandidates(unittest.TestCase):
    """Генерация не должна тратить прогоны на пустые мутации."""

    def _base(self, name: str = "B") -> Strategy:
        return Strategy(
            id=f"t ({name})", name=name, source_bat=f"t ({name}).bat",
            args=[
                "--wf-tcp=443",
                "--filter-tcp=443",
                "--hostlist={LISTS}/list-general.txt",
                "--dpi-desync=fake,multisplit",
                "--dpi-desync-split-pos=1",
                "--dpi-desync-repeats=6",
                "--dpi-desync-fooling=ts",
                "--new",
                "--filter-udp=19294-19344,50000-50100",
                "--filter-l7=discord,stun",
                "--dpi-desync=fake",
                "--dpi-desync-fake-discord={BIN}/ACTIVE_DISCORD_UDP.bin",
                "--dpi-desync-fake-stun={BIN}/ACTIVE_DISCORD_UDP.bin",
                "--dpi-desync-repeats=6",
            ],
        )

    def test_no_candidate_is_copy_of_base(self):
        base = self._base()
        cands = D.generate_candidates([base], budget=60, seed=1)
        self.assertTrue(cands, "кандидаты должны генерироваться")
        for c in cands:
            self.assertNotEqual(tuple(c.args), tuple(base.args),
                                f"{c.name} — точная копия базы")

    def test_no_duplicate_candidates(self):
        cands = D.generate_candidates([self._base()], budget=80, seed=7)
        keys = [tuple(c.args) for c in cands]
        self.assertEqual(len(keys), len(set(keys)), "в переборе есть дубли кандидатов")

    def _bare_base(self, name: str = "BARE") -> Strategy:
        """База без «ручек» вовсе — как ALT/ALT9 для pos/seqovl/blob.

        Проверяем на ней, что первый круг действительно доходит до каждой ручки:
        если у базы нет ключа, мутация обязана его добавить.
        """
        return Strategy(
            id=f"t ({name})", name=name, source_bat=f"t ({name}).bat",
            args=[
                "--wf-tcp=443",
                "--filter-tcp=443",
                "--hostlist={LISTS}/list-general.txt",
                "--dpi-desync=fake",
                "--dpi-desync-repeats=6",
                "--dpi-desync-fooling=ts",
                "--new",
                "--filter-udp=19294-19344,50000-50100",
                "--filter-l7=discord,stun",
                "--dpi-desync=fake",
                "--dpi-desync-fake-discord={BIN}/ACTIVE_DISCORD_UDP.bin",
                "--dpi-desync-fake-stun={BIN}/ACTIVE_DISCORD_UDP.bin",
            ],
        )

    def test_first_round_covers_every_knob(self):
        """Фаза 1 — по одному кандидату на каждую ручку (сравнение баз между собой)."""
        base = self._bare_base()
        all_knobs = {**D.UDP_KNOBS, **D.KNOBS}
        cands = D.generate_candidates([base], budget=60, seed=3)
        first = cands[:len(all_knobs)]
        changed: set[str] = set()
        for c in first:
            for knob in all_knobs:
                key = _knob_key(knob)
                va = [t for t in c.args if t.startswith(key + "=")]
                vb = [t for t in base.args if t.startswith(key + "=")]
                if va != vb:
                    changed.add(knob)
        self.assertEqual(changed, set(all_knobs),
                         "первый круг должен задеть каждую ручку")

    def test_knob_with_same_value_is_skipped(self):
        """Ручка, значение которой уже стоит в базе, не тратит прогон.

        Именно из-за этого 5 из 60 кандидатов были точной копией базы.
        """
        base = self._base()
        all_knobs = {**D.UDP_KNOBS, **D.KNOBS}
        cands = D.generate_candidates([base], budget=60, seed=3)
        same_only: list[str] = []
        for c in cands[:len(all_knobs)]:
            touched = []
            for knob in all_knobs:
                key = _knob_key(knob)
                va = [t for t in c.args if t.startswith(key + "=")]
                vb = [t for t in base.args if t.startswith(key + "=")]
                if va != vb:
                    touched.append(knob)
            if touched == ["pos"]:
                # pos в базе = 1, и первое значение ручки тоже 1 -> должен быть пропущен
                same_only.append(c.name)
        self.assertEqual(same_only, [], "кандидат с неизменившейся ручкой pos не должен появляться")

    def test_mutation_actually_changes_tokens(self):
        """Каждый кандидат отличается от базы по какому-то ключу ручек."""
        base = self._base()
        all_knobs = {**D.UDP_KNOBS, **D.KNOBS}
        for c in D.generate_candidates([base], budget=60, seed=5):
            touched = []
            for knob in all_knobs:
                key = _knob_key(knob)
                va = [t for t in c.args if t.startswith(key + "=")]
                vb = [t for t in base.args if t.startswith(key + "=")]
                if va != vb:
                    touched.append(knob)
            self.assertTrue(touched, f"{c.name} не изменяет ни одной ручки")

    def test_repeats_and_urepeats_are_independent(self):
        """Связка repeats/urepeats: голосовой repeats должен уметь отличаться.

        Раньше `repeats` и `urepeats` писали один ключ, и голосовая ручка молча
        затиралась — теперь urepeats адресуется только голосовой секции.
        """
        base = self._base()
        cands = D.generate_candidates([base], budget=60, seed=11)
        found_voice_only = False
        for c in cands:
            secs = D._split_sections(c.args)
            for sec in secs:
                if not D._is_voice_udp_section(sec):
                    continue
                voice_rep = D._value_in(sec, "--dpi-desync-repeats")
                tcp_reps = {
                    D._value_in(s, "--dpi-desync-repeats")
                    for s in secs if D._is_tcp_section(s)
                }
                if voice_rep is not None and tcp_reps and voice_rep not in tcp_reps:
                    found_voice_only = True
        self.assertTrue(found_voice_only,
                        "нет ни одного кандидата с независимым голосовым repeats")

    def test_effective_rejects_same_value(self):
        base = self._base()
        self.assertFalse(D._effective(base, {"method": "fake,multisplit"}),
                         "значение ручки уже стоит в базе — мутация пустая")
        self.assertTrue(D._effective(base, {"method": "multisplit"}))

    def test_effective_voice_knob_needs_voice_section(self):
        """Ручки голоса бессмысленны, если голосовой секции нет."""
        base = Strategy(id="x", name="X", source_bat="x.bat", args=[
            "--filter-tcp=443",
            "--dpi-desync=fake,multisplit",
        ])
        self.assertFalse(D._effective(base, {"urepeats": "2"}))
        self.assertFalse(D._effective(base, {"ufake": "{BIN}/stun.bin"}))
        self.assertTrue(D._effective(base, {"repeats": "2"}))

    def test_voice_fake_values_are_udp_payloads(self):
        """Голосовой фейк Discord не должен подменяться TLS-блобом.

        `--dpi-desync-fake-discord` уходит в UDP-поток Discord, поэтому подставлять
        туда TLS ClientHello (или что-то не-UDP) бессмысленно.
        """
        for value in D.UDP_KNOBS["ufake"]:
            self.assertNotIn("tls_clienthello", value,
                             "в голосовой фейк попадает TLS-блоб")


class TestRankBases(unittest.TestCase):
    """Базы выбираются по результатам пользователя, а не жёстким списком."""

    def _strategy(self, name: str) -> Strategy:
        return Strategy(id=f"id ({name})", name=name, source_bat=f"f ({name}).bat",
                        args=["--filter-tcp=443", "--dpi-desync=fake"])

    def test_working_results_come_first(self):
        pool = {n: self._strategy(n) for n in ("ALT", "ALT9", "SIMPLE FAKE")}
        from freeconnect import strategies
        with mock.patch.object(strategies, "load_strategies", return_value=list(pool.values())):
            ranked = D.rank_bases(None, [
                {"name": "SIMPLE FAKE", "discord": 3, "youtube": 3, "discord_ok": True},
                {"name": "ALT", "discord": 1, "youtube": 0},
            ])
        self.assertEqual(ranked[0].name, "SIMPLE FAKE",
                         "первой должна идти база с лучшим прошлым результатом")
        self.assertEqual(ranked[1].name, "ALT")

    def test_primary_is_first(self):
        primary = self._strategy("ALT")
        from freeconnect import strategies
        with mock.patch.object(strategies, "load_strategies", return_value=[primary]):
            ranked = D.rank_bases(primary, [{"name": "ALT", "discord": 3, "youtube": 3}])
        self.assertEqual(ranked[0].name, "ALT")

    def test_missing_strategies_do_not_crash(self):
        from freeconnect import strategies
        with mock.patch.object(strategies, "load_strategies", return_value=[]):
            self.assertEqual(D.rank_bases(None, [{"name": "ALT"}]), [])


@requires_runtime
class TestGenerateOnRealRuntime(unittest.TestCase):
    """На настоящем наборе: кандидаты не должны повторять авторские стратегии."""

    def test_no_candidate_equals_author_strategy(self):
        ups = load_strategies(include_custom=False)
        primary = next((s for s in ups if s.name == "ALT"), ups[0])
        bases = D.rank_bases(primary, None)
        cands = D.generate_candidates(bases, budget=120, seed=12345)
        self.assertGreater(len(cands), 50)
        author = {tuple(s.args) for s in ups}
        base_keys = {tuple(b.args) for b in bases}
        for c in cands:
            self.assertNotIn(tuple(c.args), author,
                             f"{c.name} повторяет авторскую стратегию")
            self.assertNotIn(tuple(c.args), base_keys,
                             f"{c.name} повторяет базу")
        keys = [tuple(c.args) for c in cands]
        self.assertEqual(len(keys), len(set(keys)), "есть дубли")


class TestDeadRunStop(unittest.TestCase):
    """«Генерируется бесконечно и ничего не создаёт» — перебор должен прекращаться."""

    def _candidates(self, n: int) -> list[Strategy]:
        return [Strategy(id=f"cand_{i}", name=f"Кандидат {i + 1}", source_bat="deepsearch(base=ALT)",
                         args=["--filter-tcp=443", "--dpi-desync=fake", f"--dpi-desync-repeats={i % 5 + 2}"])
                for i in range(n)]

    def test_stops_when_everything_is_dead(self):
        cands = self._candidates(120)
        dead = _score(cands[0], site_ok=False, voice_ok=False)
        with mock.patch.object(D, "generate_candidates", return_value=cands), \
             mock.patch.object(D, "evaluate_strategy", return_value=dead), \
             mock.patch.object(D, "Engine", return_value=mock.MagicMock()):
            out = D.deep_search(mock.MagicMock(), budget=120, dead_run_limit=10)
        self.assertLess(len(out), len(cands), "перебор не остановился на мёртвой серии")
        self.assertIn("без единого", D.last_stop_reason())

    def test_does_not_stop_while_finding_something(self):
        cands = self._candidates(12)

        def fake_eval(engine, cand, svcs, **kw):
            # каждый третий кандидат открывает сайты Discord (но не голос)
            idx = int(cand.id.split("_")[1])
            return _score(cand, site_ok=(idx % 3 == 0), voice_ok=False)

        with mock.patch.object(D, "generate_candidates", return_value=cands), \
             mock.patch.object(D, "evaluate_strategy", side_effect=fake_eval), \
             mock.patch.object(D, "Engine", return_value=mock.MagicMock()):
            out = D.deep_search(mock.MagicMock(), budget=12, dead_run_limit=4)
        self.assertEqual(D.last_stop_reason(), "",
                         "не должно быть досрочной остановки, пока что-то открывается")
        self.assertEqual(len(out), len(cands))


if __name__ == "__main__":
    unittest.main()
