from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError

from app.main import (
    IST, EmployeeIn, PunchInIn, RegularizeIn, attendance_day, compute_late_minutes,
    compute_overtime, compute_work_hours, derived_values, instant, json_value,
    shift_bounds, valid_date, valid_month, working_days,
)


def local(value):
    return datetime.fromisoformat(value).replace(tzinfo=IST)


@pytest.mark.parametrize("clock, expected", [("09:40:00", 0), ("09:40:01", 10), ("10:15:59", 45)])
def test_grace_boundary(clock, expected):
    start = local("2026-07-06T09:30:00")
    assert compute_late_minutes(local("2026-07-06T" + clock), start) == expected


def test_instant_truncation_and_nested_serialization():
    ts = instant(1783312500900)
    assert ts.microsecond == 0
    assert json_value({"changes": {"punch_in": {"from": ts, "to": None}}}) == {
        "changes": {"punch_in": {"from": 1783312500000, "to": None}}}


@pytest.mark.parametrize("value", [1783312500, 1783312500000.0, "1783312500000", True, None, 4102444800001])
def test_bad_timestamp(value):
    with pytest.raises(ValidationError):
        PunchInIn(emp_code="EMP0001", punched_at=value)


def test_omitted_timestamp_and_readonly_fields():
    assert PunchInIn(emp_code="EMP0001", work_hours=99).punched_at is None
    assert "work_hours" not in RegularizeIn(reason="fixed times", regularized_by="HR", work_hours=99).model_fields_set


def test_overnight_boundaries():
    emp = {"shift_start": "22:00", "shift_end": "06:00"}
    assert attendance_day(local("2026-07-07T05:59:59"), emp) == "2026-07-06"
    assert attendance_day(local("2026-07-07T06:00:00"), emp) == "2026-07-07"
    start, end = shift_bounds("2026-07-06", emp)
    assert end - start == timedelta(hours=8)
    assert compute_overtime(local("2026-07-07T06:29:59"), end) == 0
    assert compute_overtime(local("2026-07-07T06:30:00"), end) == 30


def test_half_up_and_half_day_use_rounded_hours():
    start = local("2026-07-06T09:30:00")
    assert compute_work_hours(start, start + timedelta(seconds=18)) == 0.01
    emp = {"shift_start": "09:30", "shift_end": "18:30"}
    doc = {"date": "2026-07-06", "status": "WFH", "punch_in": start,
           "punch_out": start + timedelta(hours=4, minutes=29, seconds=42)}
    assert derived_values(doc, emp)["work_hours"] == 4.50
    assert derived_values(doc, emp)["half_day"] is False
    doc["punch_out"] -= timedelta(seconds=1)
    assert derived_values(doc, emp)["half_day"] is True


@pytest.mark.parametrize("value", ["2026-02-29", "2026-7-01", "2026-07-01T00:00:00", "0000-01-01"])
def test_invalid_calendar_date(value):
    with pytest.raises(ValueError):
        valid_date(value)


@pytest.mark.parametrize("value", ["2026-13", "2026-7", "0000-01"])
def test_invalid_month(value):
    with pytest.raises(ValueError):
        valid_month(value)


def test_joiner_workdays():
    assert working_days("2026-07-15", "2026-07-01", "2026-07-31") == 13
    assert working_days("2026-08-01", "2026-07-01", "2026-07-31") == 0


def test_equal_shifts_invalid():
    with pytest.raises(ValidationError):
        EmployeeIn(emp_code="EMP0001", name="A", email="a@example.com", department="D",
                   joined_on="2026-01-01", shift_start="09:30", shift_end="09:30")
