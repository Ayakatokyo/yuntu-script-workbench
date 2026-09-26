"""Yuntu custom-period policy, reusable by derived skills."""

import argparse
from datetime import date, datetime, timedelta
import json
from zoneinfo import ZoneInfo


SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")
LATEST_DAY_OFFSET = 4
EARLIEST_DAY_OFFSET = 369
MAX_SPAN_DAYS = 45


def validate_custom_period(start, end, *, today=None):
    if not isinstance(start, date) or not isinstance(end, date):
        raise ValueError("Yuntu custom dates must be valid dates")
    today = datetime.now(SHANGHAI_TIMEZONE).date() if today is None else today
    if start < today - timedelta(days=EARLIEST_DAY_OFFSET):
        raise ValueError("period.start_date cannot be more than 369 days before today")
    latest = today - timedelta(days=LATEST_DAY_OFFSET)
    if end > latest:
        raise ValueError(f"period.end_date cannot be later than today-4 ({latest.isoformat()}, Asia/Shanghai)")
    if end < start or (end - start).days >= MAX_SPAN_DAYS:
        raise ValueError("period must be an inclusive range of 1 to 45 days")
    return {"start_date": start.isoformat(), "end_date": end.isoformat()}


def _parse_date(value):
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(value, fmt).date()
            if parsed.strftime(fmt) == value:
                return parsed
        except ValueError:
            pass
    raise ValueError("Yuntu custom dates must use YYYYMMDD or YYYY-MM-DD")


def main():
    parser = argparse.ArgumentParser(description="Check Yuntu T+4 before any external collection")
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    args = parser.parse_args()
    try:
        result = validate_custom_period(_parse_date(args.start_date), _parse_date(args.end_date))
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
