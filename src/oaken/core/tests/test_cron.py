# SPDX-License-Identifier: Apache-2.0
from datetime import UTC, datetime

import pytest

from oaken.core.cron import parse_cron
from oaken.core.errors import ConfigError


def at(y: int, mo: int, d: int, h: int = 0, mi: int = 0, s: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, s, tzinfo=UTC)


def nxt(expr: str, after: datetime) -> datetime:
    return parse_cron(expr).next_after(after)


def test_every_minute_is_strictly_after() -> None:
    assert nxt("* * * * *", at(2026, 1, 1, 0, 0, 30)) == at(2026, 1, 1, 0, 1)
    assert nxt("* * * * *", at(2026, 1, 1, 0, 0)) == at(2026, 1, 1, 0, 1)


def test_steps_and_lists() -> None:
    assert nxt("*/15 * * * *", at(2026, 1, 1, 0, 14)) == at(2026, 1, 1, 0, 15)
    assert nxt("0,30 * * * *", at(2026, 1, 1, 0, 30)) == at(2026, 1, 1, 1, 0)
    assert nxt("10-20/5 * * * *", at(2026, 1, 1, 0, 0)) == at(2026, 1, 1, 0, 10)
    assert nxt("5/20 * * * *", at(2026, 1, 1, 0, 6)) == at(2026, 1, 1, 0, 25)


def test_daily_rolls_over_to_next_day_month_and_year() -> None:
    assert nxt("30 3 * * *", at(2026, 1, 1, 3, 30)) == at(2026, 1, 2, 3, 30)
    assert nxt("0 0 1 * *", at(2026, 1, 31, 12)) == at(2026, 2, 1)
    assert nxt("0 0 1 1 *", at(2026, 6, 1)) == at(2027, 1, 1)


def test_names_and_sunday_as_zero_or_seven() -> None:
    # 2026-01-01 is a Thursday
    assert nxt("0 9 * * mon", at(2026, 1, 1)) == at(2026, 1, 5, 9)
    assert nxt("0 9 * * 0", at(2026, 1, 1)) == at(2026, 1, 4, 9)
    assert nxt("0 9 * * 7", at(2026, 1, 1)) == at(2026, 1, 4, 9)
    assert nxt("0 0 1 jun *", at(2026, 1, 1)) == at(2026, 6, 1)


def test_day_of_month_or_weekday_when_both_restricted() -> None:
    # the 2nd or any Monday: Mon 2026-01-05 comes after Fri 2026-01-02
    assert nxt("0 0 2 * mon", at(2026, 1, 1)) == at(2026, 1, 2)
    assert nxt("0 0 2 * mon", at(2026, 1, 2)) == at(2026, 1, 5)


def test_leap_day() -> None:
    assert nxt("0 0 29 2 *", at(2026, 1, 1)) == at(2028, 2, 29)


def test_timezone_aware_input_is_converted_to_utc() -> None:
    from datetime import timedelta, timezone

    plus2 = timezone(timedelta(hours=2))
    assert nxt("0 12 * * *", datetime(2026, 1, 1, 13, 0, tzinfo=plus2)) == at(2026, 1, 1, 12)


def test_naive_datetime_rejected() -> None:
    with pytest.raises(ValueError, match="timezone"):
        parse_cron("* * * * *").next_after(datetime(2026, 1, 1))  # noqa: DTZ001


@pytest.mark.parametrize(
    "expr",
    [
        "* * * *",
        "* * * * * *",
        "60 * * * *",
        "* 24 * * *",
        "* * 0 * *",
        "* * * 13 *",
        "* * * * 8",
        "*/0 * * * *",
        "*/x * * * *",
        "a * * * *",
        "5-2 * * * *",
        "* * * foo *",
    ],
)
def test_invalid_expressions(expr: str) -> None:
    with pytest.raises(ConfigError):
        parse_cron(expr)


def test_impossible_date_is_reported() -> None:
    with pytest.raises(ConfigError, match="never matches"):
        nxt("0 0 31 2 *", at(2026, 1, 1))
