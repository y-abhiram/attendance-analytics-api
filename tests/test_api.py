"""Integration tests require a real MongoDB 6+. Only a fresh UUID database is changed."""
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient

import app.main as main

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def api():
    mongo = MongoClient(os.getenv("TEST_MONGO_URI", "mongodb://localhost:27017"),
                        tz_aware=True, serverSelectionTimeoutMS=3000)
    mongo.admin.command("ping")
    test_db = mongo["hrone_test_" + uuid4().hex]
    original_db, original_client = main.db, main.client
    main.db, main.client = test_db, mongo
    try:
        with TestClient(main.app) as http:
            yield http, test_db
    finally:
        # The test fixture owns this unique database, never the application's database.
        cleanup = MongoClient(os.getenv("TEST_MONGO_URI", "mongodb://localhost:27017"))
        cleanup.drop_database(test_db.name)
        cleanup.close()
        main.db, main.client = original_db, original_client


@pytest.fixture(autouse=True)
def clean(api):
    _, db = api
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})


def employee(code="EMP0001", department="Engineering", joined="2026-01-01", **extra):
    return {"emp_code": code, "name": code, "email": code + "@example.com", "department": department,
            "joined_on": joined, **extra}


def millis(value):
    return int(datetime.fromisoformat(value).replace(tzinfo=main.IST).timestamp() * 1000)


def post_employee(api, **kwargs):
    response = api[0].post("/employees", json=employee(**kwargs))
    assert response.status_code == 201, response.text
    return response.json()


def log(code, day, status="PRESENT", hours=8, late=0, half=False, overtime=0):
    ts = main.instant(millis(day + "T09:30:00"))
    return {"emp_code": code, "date": day, "status": status,
            "punch_in": ts if status in main.PRESENCE else None,
            "punch_out": ts + timedelta(hours=8) if hours is not None and status in main.PRESENCE else None,
            "work_hours": hours if status in main.PRESENCE else None,
            "late_minutes": late, "overtime_minutes": overtime, "half_day": half}


def test_health_and_database_failure(api, monkeypatch):
    http, _ = api
    assert http.get("/health").json() == {"status": "ok"}
    def fail(*args, **kwargs):
        raise main.PyMongoError("unavailable")
    monkeypatch.setattr(main.db, "command", fail)
    assert http.get("/health").status_code == 503


def test_employee_paging_filter_validation_and_race(api):
    http, _ = api
    for code, dept in [("EMP0003", "Sales"), ("EMP0002", "Engineering"), ("EMP0001", "Engineering")]:
        result = post_employee(api, code=code, department=dept)
        assert isinstance(result["created_at"], int) and "_id" not in result
    page = http.get("/employees", params={"department": "Engineering", "page_size": 1}).json()
    assert page["total"] == 2 and page["items"][0]["emp_code"] == "EMP0001"
    assert http.get("/employees", params={"page": 0}).status_code == 422
    assert http.get("/employees", params={"page_size": 101}).status_code == 422
    for fields in [{"emp_code": "x"}, {"email": "x"}, {"shift_start": "25:00"}, {"joined_on": "2026-02-30"}]:
        assert http.post("/employees", json={**employee("EMP0009"), **fields}).status_code == 422
    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(lambda _: http.post("/employees", json=employee("EMP0010")).status_code, range(8)))
    assert codes.count(201) == 1 and codes.count(409) == 7


def test_punch_rules_overnight_and_races(api):
    http, _ = api
    post_employee(api, shift_start="22:00", shift_end="06:00")
    payload = {"emp_code": "EMP0001", "punched_at": millis("2026-07-06T21:55:00") + 999}
    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(lambda _: http.post("/attendance/punch-in", json=payload).status_code, range(8)))
    assert codes.count(201) == 1 and codes.count(409) == 7
    payload["punched_at"] = millis("2026-07-07T06:40:00")
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: http.post("/attendance/punch-out", json=payload), range(8)))
    assert sum(r.status_code == 200 for r in results) == 1
    assert sum(r.status_code == 409 for r in results) == 7
    doc = next(r.json() for r in results if r.status_code == 200)
    assert doc["date"] == "2026-07-06" and doc["work_hours"] == 8.75
    assert doc["overtime_minutes"] == 40 and doc["history"] == []
    assert "id" not in doc and "_id" not in doc


def test_punch_validation_missing_and_duration(api):
    http, _ = api
    assert http.post("/attendance/punch-in", json={"emp_code": "unknown"}).status_code == 404
    post_employee(api)
    assert http.post("/attendance/punch-out", json={"emp_code": "EMP0001"}).status_code == 404
    ts = millis("2026-07-06T09:40:01")
    for value in [ts // 1000, str(ts), float(ts), None]:
        assert http.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": value}).status_code == 422
    assert http.post("/attendance/punch-in", json={"emp_code": "EMP0001", "status": "ABSENT"}).status_code == 422
    response = http.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ts})
    assert response.json()["late_minutes"] == 10
    for out in [ts, ts + 86401000]:
        assert http.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": out}).status_code == 422


def test_regularization_audit_validation_and_concurrency(api, monkeypatch):
    http, db = api
    post_employee(api)
    ts = millis("2026-07-06T10:05:00")
    http.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ts})
    url = "/attendance/EMP0001/2026-07-06"
    common = {"reason": "biometric glitch", "regularized_by": "HR"}
    fixed = http.patch(url, json={**common, "punch_in": millis("2026-07-06T09:28:00"), "work_hours": 99})
    assert fixed.status_code == 200, fixed.text
    doc = fixed.json()
    assert set(doc["history"][0]["changes"]) == {"punch_in", "late_minutes"}
    assert doc["history"][0]["changes"]["punch_in"]["from"] == ts
    assert http.patch(url, json=common).status_code == 422
    assert http.patch(url, json={**common, "punch_in": millis("2026-07-07T09:30:00")}).status_code == 422
    assert http.patch(url, json={**common, "status": "LEAVE", "punch_out": ts}).status_code == 422
    assert http.patch(url, json={**common, "punch_out": ts - 86400000}).status_code == 422
    assert http.patch(url, json={**common, "punch_out": None}).status_code == 422
    # Force both corrections to read the same snapshot before either can update.
    from threading import Barrier
    barrier = Barrier(2)
    original_filter = main.snapshot_filter
    def simultaneous_filter(doc):
        result = original_filter(doc)
        barrier.wait(timeout=5)
        return result
    with monkeypatch.context() as patch:
        patch.setattr(main, "snapshot_filter", simultaneous_filter)
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda status: http.patch(url, json={**common, "status": status}), ["WFH", "ON_DUTY"]))
    assert sorted(r.status_code for r in responses) == [200, 409]
    stored = db.attendance_logs.find_one({"emp_code": "EMP0001"})
    assert len(stored["history"]) == 1 + sum(r.status_code == 200 for r in responses)
    leave = http.patch(url, json={**common, "status": "LEAVE"}).json()
    assert leave["punch_in"] is None and leave["punch_out"] is None
    assert leave["work_hours"] is None and leave["late_minutes"] == 0
    assert http.patch(url, json={**common, "status": "PRESENT"}).status_code == 422


def test_attendance_filter_sort_and_legacy_defaults(api):
    http, db = api
    db.attendance_logs.insert_many([log("EMP0002", "2026-07-07"), log("EMP0001", "2026-07-07"), log("EMP0001", "2026-07-06")])
    db.attendance_logs.update_many({}, {"$unset": {"half_day": "", "late_minutes": ""}})
    page = http.get("/attendance", params={"date_from": "2026-07-07", "page_size": 1}).json()
    assert page["total"] == 2 and page["items"][0]["emp_code"] == "EMP0001"
    assert page["items"][0]["history"] == [] and page["items"][0]["half_day"] is False
    for params in [{"date_from": "2026-07-08", "date_to": "2026-07-07"}, {"date_from": "2026-02-30"}, {"status": "bad"}]:
        assert http.get("/attendance", params=params).status_code == 422


def seed_analytics(api):
    for code, joined in [("EMP0001", "2026-01-01"), ("EMP0002", "2026-07-15"),
                         ("EMP0003", "2026-01-01"), ("EMP0004", "2026-08-01")]:
        post_employee(api, code=code, joined=joined)
    post_employee(api, code="EMP0005", department="Sales")
    api[1].attendance_logs.insert_many([
        log("EMP0001", "2026-07-06", hours=8.125, late=20),
        log("EMP0001", "2026-07-07", status="WFH", hours=3, half=True, late=10),
        log("EMP0001", "2026-07-11", late=30, overtime=40),
        log("EMP0001", "2026-07-08", status="LEAVE"),
        log("EMP0002", "2026-07-15", hours=4, late=30),
        log("EMP0003", "2026-07-06", status="ON_DUTY", hours=None, late=30),
        log("EMP0005", "2026-07-06", late=100),
        log("EMP9999", "2026-07-06", late=999),
    ])


def test_monthly_and_department_analytics(api):
    http, _ = api
    seed_analytics(api)
    monthly = http.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-07"}).json()
    assert monthly == {"emp_code": "EMP0001", "month": "2026-07", "working_days": 23,
                       "present_days": 1.5, "leave_days": 1, "late_count": 3,
                       "total_late_minutes": 60, "total_overtime_minutes": 40, "attendance_pct": 6.52}
    assert http.get("/analytics/employees/EMP0002/monthly", params={"month": "2026-07"}).json()["working_days"] == 13
    assert http.get("/analytics/employees/EMP0004/monthly", params={"month": "2026-07"}).json()["attendance_pct"] is None
    summary = http.get("/analytics/departments/summary", params={"month": "2026-07"}).json()["items"]
    eng = summary[0]
    assert eng == {"department": "Engineering", "headcount": 3, "present_days": 3.5,
                   "avg_work_hours": 5.78, "late_count": 5, "total_late_minutes": 120,
                   "leave_count": 1, "on_duty_count": 1}
    assert summary[1]["department"] == "Sales"
    assert http.get("/analytics/departments/summary", params={"month": "2026-06"}).json()["items"][0]["headcount"] == 2
    assert http.get("/analytics/employees/unknown/monthly", params={"month": "2026-07"}).status_code == 404


def test_leaderboard_ties_and_department_ranking(api):
    http, _ = api
    seed_analytics(api)
    url = "/analytics/leaderboard/late"
    rows = http.get(url, params={"month": "2026-07", "department": "Engineering", "limit": 2}).json()["items"]
    assert [r["emp_code"] for r in rows] == ["EMP0001", "EMP0002", "EMP0003"]
    assert [r["rank"] for r in rows] == [1, 2, 2]
    rows = http.get(url, params={"month": "2026-07", "limit": 3}).json()["items"]
    assert [r["rank"] for r in rows] == [1, 2, 3, 3]
    post_employee(api, code="EMP0006", department="Sales")
    api[1].attendance_logs.insert_one(log("EMP0006", "2026-07-06", late=10))
    rows = http.get(url, params={"month": "2026-07", "limit": 50}).json()["items"]
    assert [r["rank"] for r in rows] == [1, 2, 3, 3, 5]
    assert http.get(url, params={"month": "2026-07", "limit": 51}).status_code == 422


def test_trend_gaps_weekends_joiners_and_window(api):
    http, _ = api
    seed_analytics(api)
    url = "/analytics/departments/Engineering/trend"
    rows = http.get(url, params={"from": "2026-07-06", "to": "2026-07-15"}).json()["items"]
    assert len(rows) == 10
    assert rows[0]["headcount"] == 2 and rows[0]["present_count"] == 2 and rows[0]["attendance_rate"] == 1
    assert rows[1]["attendance_rate"] == 0.25 and rows[1]["moving_avg_7d"] == 0.625
    assert rows[2]["attendance_rate"] == 0 and rows[2]["moving_avg_7d"] == 0.4167
    assert rows[5]["present_count"] == 1 and rows[5]["attendance_rate"] is None
    assert rows[7]["moving_avg_7d"] == 0.05  # window excludes the first Monday
    assert rows[9]["headcount"] == 3 and rows[9]["attendance_rate"] == 0.3333
    empty = http.get(url, params={"from": "2026-06-01", "to": "2026-06-03"}).json()["items"]
    assert len(empty) == 3 and all(r["attendance_rate"] == 0 for r in empty)
    future = http.get(url, params={"from": "2025-01-01", "to": "2025-01-02"}).json()["items"]
    assert all(r["headcount"] == 0 and r["moving_avg_7d"] is None for r in future)
    for params in [{"from": "2026-07-08", "to": "2026-07-07"}, {"from": "2026-01-01", "to": "2026-05-01"}]:
        assert http.get(url, params=params).status_code == 422
    assert http.get("/analytics/departments/unknown/trend", params={"from": "2026-07-01", "to": "2026-07-02"}).status_code == 404


@pytest.mark.parametrize("endpoint,params", [
    ("attendance_list", {}),
    ("attendance_list", {"emp_code": "EMP0001", "status": "PRESENT", "page": 2, "page_size": 1}),
    ("employee_monthly", {"emp_code": "EMP0001", "month": "2026-07"}),
    ("department_summary", {"month": "2026-07"}),
    ("department_summary", {"month": "2026-07", "department": "Engineering"}),
    ("late_leaderboard", {"month": "2026-07", "department": "Engineering"}),
    ("department_trend", {"department": "Engineering", "from": "2026-07-06", "to": "2026-07-15"}),
])
def test_explain_uses_indexes(api, endpoint, params):
    seed_analytics(api)
    response = api[0].get("/admin/explain/" + endpoint, params=params)
    assert response.status_code == 200, response.text
    assert "IXSCAN" in response.text and "COLLSCAN" not in response.text


def test_explain_required_parameters(api):
    for endpoint in ["employee_monthly", "department_summary", "late_leaderboard", "department_trend"]:
        assert api[0].get("/admin/explain/" + endpoint).status_code == 422


@pytest.mark.scale
def test_100000_records(api):
    """Exercise real endpoint results and all explain plans on the required scale."""
    from time import perf_counter
    http, db = api
    db.employees.insert_many([
        {**employee(f"EMP{index:06d}", department=f"Dept{index % 10}"),
         "shift_start": "09:30", "shift_end": "18:30", "created_at": main.instant()}
        for index in range(1, 1001)
    ])
    first = datetime(2026, 6, 1, tzinfo=timezone.utc)
    for batch in range(10):
        db.attendance_logs.insert_many([
            {"emp_code": f"EMP{index:06d}", "date": (first + timedelta(days=offset)).date().isoformat(),
             "status": "PRESENT", "work_hours": 8.0, "late_minutes": (index % 5) * 10,
             "overtime_minutes": 0, "half_day": False, "history": [],
             "punch_in": first + timedelta(days=offset, hours=4),
             "punch_out": first + timedelta(days=offset, hours=12)}
            for index in range(batch * 100 + 1, batch * 100 + 101)
            for offset in range(100)
        ])
    assert db.attendance_logs.count_documents({}) == 100000
    requests = [
        ("attendance_list", "/attendance", {"date_from": "2026-07-01", "date_to": "2026-07-31", "page": 3}),
        ("employee_monthly", "/analytics/employees/EMP000001/monthly", {"emp_code": "EMP000001", "month": "2026-07"}),
        ("department_summary", "/analytics/departments/summary", {"month": "2026-07"}),
        ("late_leaderboard", "/analytics/leaderboard/late", {"month": "2026-07", "limit": 10}),
        ("department_trend", "/analytics/departments/Dept1/trend", {"department": "Dept1", "from": "2026-07-01", "to": "2026-07-31"}),
    ]
    results = {}
    for endpoint, path, params in requests:
        started = perf_counter()
        response = http.get(path, params=params)
        elapsed = perf_counter() - started
        assert response.status_code == 200, response.text
        results[endpoint] = response.json()
        print(f"{endpoint}: {elapsed:.3f}s on 100,000 records")
        explained = http.get("/admin/explain/" + endpoint, params=params)
        assert explained.status_code == 200, explained.text
        assert "IXSCAN" in explained.text and "COLLSCAN" not in explained.text
    assert results["attendance_list"]["total"] == 31000
    monthly = results["employee_monthly"]
    assert monthly["working_days"] == 23 and monthly["present_days"] == 23
    assert monthly["attendance_pct"] == 100 and monthly["total_late_minutes"] == 310
    summary = results["department_summary"]["items"]
    assert len(summary) == 10
    assert all(r["headcount"] == 100 and r["present_days"] == 2300 and r["avg_work_hours"] == 8 for r in summary)
    board = results["late_leaderboard"]["items"]
    assert len(board) == 200 and all(r["rank"] == 1 and r["total_late_minutes"] == 1240 for r in board)
    trend = results["department_trend"]["items"]
    assert len(trend) == 31 and all(r["headcount"] == 100 and r["present_count"] == 100 for r in trend)
    assert all(r["attendance_rate"] == (1 if r["is_working_day"] else None) for r in trend)
