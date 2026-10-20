"""Official snapshot pricing checks: peak, idle, holidays and unknown values."""
from datetime import datetime, timedelta, timezone
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "native"))
from codex_monitor import pricing
from codex_monitor.core import cost_value, Row

BEIJING = timezone(timedelta(hours=8))


def at(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=BEIJING).timestamp()


def item(model="deepseek-v4.1-flash", when=None, inp=1200, out=80, cache=900):
    return {"model": model, "at": when if when is not None else at(2026, 10, 19, 10),
            "input": inp, "output": out, "cache": cache}


class PricingTests(unittest.TestCase):
    def test_snapshot_identity(self):
        self.assertEqual(pricing.PRICE_VERSION, "2026-10-19-v1")
        self.assertEqual(pricing.CHECKED, "2026-10-19")
        self.assertEqual(pricing.USD_CNY, 6.8)
        self.assertTrue(any("api-docs.deepseek.com" in url for url in pricing.SOURCE_URLS))
        self.assertTrue(any("developers.openai.com" in url for url in pricing.SOURCE_URLS))

    def test_flash_peak_and_idle_use_fixed_fx(self):
        peak = pricing.cost(item(when=at(2026, 10, 19, 9)))
        expected = ((1200 - 900) * 2 + 900 * .04 + 80 * 8) / 1_000_000 / 6.8
        self.assertAlmostEqual(peak, expected)
        idle = pricing.cost(item(when=at(2026, 10, 19, 12)))
        self.assertAlmostEqual(idle, ((1200 - 900) * 1 + 900 * .02 + 80 * 4)
                               / 1_000_000 / 6.8)
        self.assertLess(idle, peak)

    def test_peak_boundaries_and_weekends(self):
        for hour, minute, period in ((9, 0, "peak"), (11, 59, "peak"),
                                     (12, 0, "idle"), (14, 0, "peak"),
                                     (17, 59, "peak"), (18, 0, "idle")):
            self.assertEqual(pricing.period(at(2026, 10, 19, hour, minute)), period)
        # Saturday, including any make-up workday, is idle.
        self.assertEqual(pricing.period(at(2026, 10, 17, 10)), "idle")
        self.assertEqual(pricing.period(at(2026, 10, 18, 10)), "idle")

    def test_2026_holidays_are_inclusive_and_idle(self):
        for year, month, day in ((2026, 1, 1), (2026, 2, 23), (2026, 4, 6),
                                 (2026, 5, 5), (2026, 6, 19), (2026, 9, 27),
                                 (2026, 10, 1), (2026, 10, 7)):
            self.assertEqual(pricing.period(at(year, month, day, 10)), "idle")
        self.assertEqual(pricing.period(at(2026, 10, 8, 10)), "peak")

    def test_outside_calendar_peak_is_unknown_but_off_peak_is_idle(self):
        self.assertIsNone(pricing.period(at(2027, 10, 19, 10)))
        self.assertIsNone(pricing.cost(item(when=at(2027, 10, 19, 10))))
        self.assertEqual(pricing.period(at(2027, 10, 19, 20)), "idle")
        self.assertIsNotNone(pricing.cost(item(when=at(2027, 10, 19, 20))))

    def test_exact_aliases_and_unknown_values(self):
        for model in ("deepseek-flash", "deepseek-v4.1-flash",
                      "deepseek-v4.1-flash-1017", "deepseek-v4-flash",
                      "deepseek-v4-flash-vision-exp"):
            self.assertIsNotNone(pricing.cost(item(model=model)))
        self.assertIsNotNone(pricing.cost(item(model="deepseek-v4-pro")))
        self.assertIsNotNone(pricing.cost(item(model="deepseek-v4-pro-0813")))
        self.assertIsNone(pricing.cost(item(model="deepseek-v4")))
        self.assertIsNone(pricing.cost(item(model="x-deepseek-v4.1-flash")))
        self.assertIsNone(pricing.cost(item(cache=None)))
        self.assertIsNone(pricing.cost(item(cache=5000)))
        self.assertIsNone(pricing.cost(item(out=-1)))

    def test_gpt_uses_standard_core_reference(self):
        value = item(model="gpt-6.1-sol", when=at(2026, 10, 19, 10))
        expected = cost_value(Row(key="", at=0, model="gpt-6.1-sol",
                                  usage={"input_tokens": 1200, "cached_input_tokens": 900,
                                         "output_tokens": 80},
                                  duration=None, tier="standard", tier_source="unknown"))
        self.assertAlmostEqual(pricing.cost(value), expected)
        self.assertIsNone(pricing.cost(item(model="gpt-7-unknown")))
        self.assertEqual(pricing.basis(value), "OpenAI 官方价格（标准）")

    def test_basis_is_concise_and_never_claims_a_bill(self):
        self.assertIn("DeepSeek", pricing.basis(item(when=at(2026, 10, 19, 10))))
        self.assertIn("峰时", pricing.basis(item(when=at(2026, 10, 19, 10))))
        self.assertIn("闲时", pricing.basis(item(when=at(2026, 10, 19, 12))))
        self.assertIsNone(pricing.basis(item(model="unknown")))


if __name__ == "__main__":
    unittest.main()
