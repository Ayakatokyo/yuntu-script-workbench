import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from date_policy import validate_custom_period  # noqa: E402
from scripts.config_specs import ConfigSpecError, resolve_query_spec  # noqa: E402


class DatePolicyTests(unittest.TestCase):
    def test_query_spec_rejects_t_minus_3_before_collection(self):
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        end = today - timedelta(days=3)
        with self.assertRaisesRegex(ConfigSpecError, "today-4"):
            resolve_query_spec({
                "schema_version": 1,
                "source": "juliang_yuntu_industry_content_rankings",
                "industry": "食品饮料",
                "period": {"type": "CUSTOM", "start_date": (end - timedelta(days=1)).strftime("%Y%m%d"), "end_date": end.strftime("%Y%m%d")},
                "ranking": {"metric": "CTR_TOP1000"},
            })

    def test_t_plus_4_and_range_boundaries(self):
        today = date(2026, 9, 25)
        self.assertEqual(validate_custom_period(date(2026, 9, 20), date(2026, 9, 21), today=today)["end_date"], "2026-09-21")
        for end in (date(2026, 9, 22), date(2026, 9, 25), date(2026, 9, 26)):
            with self.subTest(end=end), self.assertRaisesRegex(ValueError, "today-4"):
                validate_custom_period(date(2026, 9, 20), end, today=today)
        with self.assertRaisesRegex(ValueError, "369"):
            validate_custom_period(date(2025, 9, 20), date(2025, 9, 21), today=today)
        with self.assertRaisesRegex(ValueError, "1 to 45"):
            validate_custom_period(date(2026, 7, 1), date(2026, 8, 15), today=today)


if __name__ == "__main__":
    unittest.main()
