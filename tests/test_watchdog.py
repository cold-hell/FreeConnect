"""Логика порога деградации в ServiceWatchdog (без сети)."""
import unittest

from freeconnect.watchdog import ServiceWatchdog


def _wd(fail_limit=2):
    # services_provider не используется в этих тестах (дёргаем record напрямую).
    return ServiceWatchdog(services_provider=lambda: [], fail_limit=fail_limit)


class TestWatchdogThreshold(unittest.TestCase):
    def test_fires_only_after_fail_limit(self):
        wd = _wd(fail_limit=2)
        self.assertFalse(wd.record("discord", ok=False))  # 1-я неудача — рано
        self.assertTrue(wd.record("discord", ok=False))   # 2-я подряд — деградация

    def test_counter_resets_after_fire(self):
        wd = _wd(fail_limit=2)
        wd.record("discord", ok=False)
        self.assertTrue(wd.record("discord", ok=False))   # сработало и сбросилось
        self.assertFalse(wd.record("discord", ok=False))  # снова считаем с нуля
        self.assertTrue(wd.record("discord", ok=False))

    def test_success_resets_streak(self):
        wd = _wd(fail_limit=2)
        self.assertFalse(wd.record("youtube", ok=False))
        self.assertFalse(wd.record("youtube", ok=True))   # успех обнуляет серию
        self.assertFalse(wd.record("youtube", ok=False))  # опять только 1-я неудача
        self.assertTrue(wd.record("youtube", ok=False))

    def test_services_are_independent(self):
        wd = _wd(fail_limit=2)
        self.assertFalse(wd.record("discord", ok=False))
        self.assertFalse(wd.record("youtube", ok=False))  # у youtube своя серия
        self.assertTrue(wd.record("discord", ok=False))   # discord добрал первым
        self.assertTrue(wd.record("youtube", ok=False))

    def test_higher_limit(self):
        wd = _wd(fail_limit=3)
        self.assertFalse(wd.record("discord", ok=False))
        self.assertFalse(wd.record("discord", ok=False))
        self.assertTrue(wd.record("discord", ok=False))   # только 3-я подряд


class TestWatchdogProbeSelection(unittest.TestCase):
    def test_probe_unknown_service_is_ok(self):
        # Нет целей в DEFAULT_TARGETS -> считаем «доступно», не шумим.
        wd = _wd()
        ok, status = wd._probe("nonexistent-service")
        self.assertTrue(ok)
        self.assertEqual(status, "")


if __name__ == "__main__":
    unittest.main()
