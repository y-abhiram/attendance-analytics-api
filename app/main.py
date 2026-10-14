"""Employee Attendance API. Run: uvicorn app.main:app --port 8000.

Dates identify IST attendance days; instants are UTC BSON dates internally and
integer epoch milliseconds at the HTTP boundary. All analytics run in MongoDB.
"""
import calendar
import os
import re
from contextlib import asynccontextmanager
from datetime import date as Date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal

from bson import Decimal128, ObjectId
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import AfterValidator, BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv(override=False)
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
EpochMillis = Annotated[int, Field(strict=True, ge=100000000000, le=4102444800000)]
Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]
Limit = Annotated[int, Query(ge=1, le=50)]


def valid_date(value: str) -> str:
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("Use YYYY-MM-DD")
    Date.fromisoformat(value)
    return value


def valid_month(value: str) -> str:
    if not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", value):
        raise ValueError("Use YYYY-MM")
    Date.fromisoformat(value + "-01")
    return value


DateString = Annotated[str, AfterValidator(valid_date)]
MonthString = Annotated[str, AfterValidator(valid_month)]
ShiftTime = Annotated[str, Field(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")]

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True, serverSelectionTimeoutMS=3000, connectTimeoutMS=3000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Index names are also used as hints by the real queries and explain endpoint.
    db.employees.create_index([("emp_code", 1)], unique=True, name="employee_code")
    db.employees.create_index([("department", 1), ("emp_code", 1)], name="department_code")
    db.employees.create_index([("joined_on", 1), ("department", 1)], name="joined_department")
    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", -1)], unique=True, name="employee_date",
    )
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)], name="date_employee")
    db.attendance_logs.create_index(
        [("status", 1), ("date", -1), ("emp_code", 1)], name="status_date_employee",
    )
    db.attendance_logs.create_index(
        [("emp_code", 1), ("punch_in", -1)], name="employee_punch",
    )
    yield
    client.close()


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def database_error(request: Request, exc: PyMongoError):
    return JSONResponse(status_code=503, content={"detail": "MongoDB unavailable"})


# --------------------------- Validation and time rules --------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: ShiftTime = "09:30"
    shift_end: ShiftTime = "18:30"
    joined_on: DateString

    @model_validator(mode="after")
    def different_shift_times(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: EpochMillis | None = None

    @field_validator("punched_at", mode="before")
    @classmethod
    def non_null_timestamp(cls, value):
        if value is None:
            raise ValueError("Omit punched_at to use now; explicit null is invalid")
        return value


class PunchInIn(PunchOutIn):
    status: PresenceStatus = "PRESENT"


class RegularizeIn(BaseModel):
    status: Status | None = None
    punch_in: EpochMillis | None = None
    punch_out: EpochMillis | None = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("status", "punch_in", "punch_out", mode="before")
    @classmethod
    def no_explicit_null(cls, value):
        if value is None:
            raise ValueError("Omit unchanged fields; explicit null is invalid")
        return value


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def instant(value: int | None = None) -> datetime:
    ts = datetime.now(UTC) if value is None else EPOCH + timedelta(milliseconds=value)
    return ts.replace(microsecond=0)


def json_value(value):
    """Convert nested BSON values, including dates inside history changes."""
    if isinstance(value, datetime):
        return int((utc(value) - EPOCH) // timedelta(milliseconds=1))
    if isinstance(value, Decimal128):
        return float(value.to_decimal())
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, dict):
        return {key: json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    return value


def employee_json(doc):
    return json_value({key: value for key, value in doc.items() if key != "_id"})


def attendance_json(doc):
    defaults = {"punch_in": None, "punch_out": None, "work_hours": None,
                "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": []}
    defaults.update({key: value for key, value in doc.items() if key != "_id"})
    return json_value(defaults)


def employee(emp_code):
    result = db.employees.find_one({"emp_code": emp_code})
    if result is None:
        raise HTTPException(404, "Employee not found")
    return result


def attendance_day(punch_in: datetime, emp) -> str:
    local = utc(punch_in).astimezone(IST)
    day = local.date()
    if emp["shift_end"] <= emp["shift_start"] and local.time() < time.fromisoformat(emp["shift_end"]):
        day -= timedelta(days=1)
    return day.isoformat()


def shift_bounds(date_str: str, emp):
    day = Date.fromisoformat(date_str)
    end_day = day + timedelta(days=int(emp["shift_end"] <= emp["shift_start"]))
    return (datetime.combine(day, time.fromisoformat(emp["shift_start"]), IST),
            datetime.combine(end_day, time.fromisoformat(emp["shift_end"]), IST))


def compute_late_minutes(punch_in: datetime, shift_start: datetime) -> int:
    seconds = int((utc(punch_in) - utc(shift_start)).total_seconds())
    return seconds // 60 if seconds > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((utc(punch_out) - utc(punch_in)).total_seconds())
    return float((Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, shift_end: datetime) -> int:
    minutes = int((utc(punch_out) - utc(shift_end)).total_seconds()) // 60
    return minutes if minutes >= 30 else 0


def derived_values(doc, emp):
    if doc["status"] not in PRESENCE:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    start, end = shift_bounds(doc["date"], emp)
    result = {"work_hours": None, "late_minutes": compute_late_minutes(doc["punch_in"], start),
              "overtime_minutes": 0, "half_day": False}
    if doc.get("punch_out") is not None:
        hours = compute_work_hours(doc["punch_in"], doc["punch_out"])
        result.update(work_hours=hours, overtime_minutes=compute_overtime(doc["punch_out"], end),
                      half_day=hours < 4.50)
    return result


def validate_duration(punch_in, punch_out):
    seconds = (utc(punch_out) - utc(punch_in)).total_seconds()
    if seconds <= 0 or seconds > 86400:
        raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")


EDIT_FIELDS = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")


def snapshot_filter(doc):
    """Compare-and-set on the observed state, without adding fields to the schema.

    Include history to detect even edits that changed a record away and back.
    Missing legacy fields are matched with $exists rather than silently defaulted.
    """
    return {"_id": doc["_id"], **{
        key: {"$eq": doc[key], "$exists": True} if key in doc else {"$exists": False}
        for key in (*EDIT_FIELDS, "history")
    }}


# --------------------------- Employee and attendance endpoints ------------- #
@app.get("/health")
def health():
    try:
        db.command("ping")
    except PyMongoError:
        raise HTTPException(503, "MongoDB unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = instant()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_json(doc)


@app.get("/employees")
def list_employees(department: str | None = None, page: Page = 1, page_size: PageSize = 20):
    query = {} if department is None else {"department": department}
    hint = "employee_code" if department is None else "department_code"
    items = db.employees.find(query, {"_id": 0}).hint(hint).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_json(doc) for doc in items],
            "total": db.employees.count_documents(query, hint=hint), "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = employee(body.emp_code)
    ts = instant(body.punched_at)
    doc = {"emp_code": body.emp_code, "date": attendance_day(ts, emp), "status": body.status,
           "punch_in": ts, "punch_out": None, "history": []}
    doc.update(derived_values(doc, emp))
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "Record already exists for this attendance date")
    return attendance_json(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = employee(body.emp_code)
    ts = instant(body.punched_at)
    doc = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": ts}},
        sort=[("punch_in", -1)], hint="employee_punch",
    )
    if doc is None:
        raise HTTPException(404, "No punch-in found")
    if doc.get("punch_out") is not None:
        raise HTTPException(409, "Already punched out")
    validate_duration(doc["punch_in"], ts)
    final = {**doc, "punch_out": ts}
    updates = {"punch_out": ts, **derived_values(final, emp)}
    result = db.attendance_logs.find_one_and_update(
        snapshot_filter(doc), {"$set": updates}, return_document=ReturnDocument.AFTER,
    )
    if result is None:
        raise HTTPException(409, "Record changed concurrently; read it again")
    return attendance_json(result)


def attendance_query(emp_code=None, date_from=None, date_to=None, status=None):
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(422, "date_from must be on or before date_to")
    query = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        query["date"] = {}
        if date_from is not None:
            query["date"]["$gte"] = date_from
        if date_to is not None:
            query["date"]["$lte"] = date_to
    if status is not None:
        query["status"] = status
    hint = "employee_date" if emp_code is not None else (
        "status_date_employee" if status is not None else "date_employee")
    return query, hint


@app.get("/attendance")
def list_attendance(emp_code: str | None = None, date_from: DateString | None = None,
                    date_to: DateString | None = None, status: Status | None = None,
                    page: Page = 1, page_size: PageSize = 20):
    query, hint = attendance_query(emp_code, date_from, date_to, status)
    docs = db.attendance_logs.find(query, {"_id": 0}).hint(hint).sort(
        [("date", -1), ("emp_code", 1)]).skip((page - 1) * page_size).limit(page_size)
    return {"items": [attendance_json(doc) for doc in docs],
            "total": db.attendance_logs.count_documents(query, hint=hint), "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: DateString, body: RegularizeIn):
    emp = employee(emp_code)
    original = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if original is None:
        raise HTTPException(404, "Attendance record not found")
    final = {**original}
    supplied = body.model_fields_set
    if "status" in supplied:
        final["status"] = body.status
    if final["status"] not in PRESENCE:
        if supplied & {"punch_in", "punch_out"}:
            raise HTTPException(422, "ABSENT/LEAVE cannot have punch times")
        final.update(punch_in=None, punch_out=None)
    else:
        for key in ("punch_in", "punch_out"):
            if key in supplied:
                final[key] = instant(getattr(body, key))
            elif final.get(key) is not None:
                final[key] = utc(final[key]).replace(microsecond=0)
        if final.get("punch_in") is None:
            raise HTTPException(422, "Presence status requires punch_in")
        if attendance_day(final["punch_in"], emp) != date:
            raise HTTPException(422, "punch_in must remain on the attendance date")
        if final.get("punch_out") is not None:
            validate_duration(final["punch_in"], final["punch_out"])
    final.update(derived_values(final, emp))
    defaults = {"punch_in": None, "punch_out": None, "work_hours": None,
                "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    changes = {key: {"from": original.get(key, defaults.get(key)), "to": final.get(key)}
               for key in EDIT_FIELDS if original.get(key, defaults.get(key)) != final.get(key)}
    if not changes:
        raise HTTPException(422, "Correction changes nothing")
    history = {"at": instant(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    result = db.attendance_logs.find_one_and_update(
        snapshot_filter(original),
        {"$set": {key: final.get(key) for key in EDIT_FIELDS}, "$push": {"history": history}},
        return_document=ReturnDocument.AFTER,
    )
    if result is None:
        raise HTTPException(409, "Record changed concurrently; read it again")
    return attendance_json(result)


# --------------------------- Shared aggregation builders ------------------- #
def month_bounds(month):
    year, number = map(int, month.split("-"))
    return f"{month}-01", f"{month}-{calendar.monthrange(year, number)[1]:02}"


def working_days(joined_on, first, last):
    """Calendar arithmetic only; no attendance documents are processed in Python."""
    start, end = max(Date.fromisoformat(joined_on), Date.fromisoformat(first)), Date.fromisoformat(last)
    if start > end:
        return 0
    weeks, remainder = divmod((end - start).days + 1, 7)
    return weeks * 5 + sum((start.weekday() + offset) % 7 < 5 for offset in range(remainder))


def round_half_up(expression, places=2):
    """MongoDB $round uses ties-to-even; nonnegative metrics need half-up instead.

    Decimal arithmetic avoids floating-point half-boundary errors. Null stays null.
    """
    scale = 10 ** places
    return {"$let": {"vars": {"value": expression}, "in": {"$cond": [
        {"$eq": ["$$value", None]}, None,
        {"$toDouble": {"$divide": [
            {"$floor": {"$add": [{"$multiply": [{"$toDecimal": "$$value"}, scale]}, Decimal128("0.5")]}}, scale,
        ]}},
    ]}}}


def is_weekday(day_expression):
    return {"$in": [{"$dayOfWeek": day_expression}, [2, 3, 4, 5, 6]]}


def presence_weight(weekdays_only=True):
    conditions = [{"$in": ["$status", PRESENCE]}]
    if weekdays_only:
        conditions.append(is_weekday({"$dateFromString": {"dateString": "$date"}}))
    return {"$cond": [{"$and": conditions},
                      {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}


def late_flag():
    return {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}


def stats_group():
    return {"$group": {
        "_id": None,
        "present_days": {"$sum": presence_weight()},
        "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
        "late_count": {"$sum": late_flag()},
        "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
        "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
    }}


def monthly_pipeline(emp_code, month):
    first, last = month_bounds(month)
    return [{"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
            stats_group(), {"$project": {"_id": 0}}]


def summary_pipeline(month, department=None):
    first, last = month_bounds(month)
    match = {"joined_on": {"$lte": last}}
    if department is not None:
        match["department"] = department
    group = stats_group()["$group"]
    group = {**group, "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
             "hours_sum": {"$sum": {"$cond": [{"$in": ["$status", PRESENCE]},
                                               {"$toDecimal": {"$ifNull": ["$work_hours", 0]}}, 0]}},
             "hours_count": {"$sum": {"$cond": [{"$and": [
                 {"$in": ["$status", PRESENCE]}, {"$ne": [{"$ifNull": ["$work_hours", None]}, None]},
             ]}, 1, 0]}}}
    return [
        {"$match": match},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
            {"$match": {"date": {"$gte": first, "$lte": last}, "$expr": {"$eq": ["$emp_code", "$$code"]}}},
            {"$group": group},
        ], "as": "stats"}},
        {"$set": {"stats": {"$ifNull": [{"$arrayElemAt": ["$stats", 0]}, {}]}}},
        {"$group": {"_id": "$department", "headcount": {"$sum": 1}, **{
            field: {"$sum": {"$ifNull": [f"$stats.{source}", 0]}}
            for field, source in {"present_days": "present_days", "late_count": "late_count",
                                  "total_late_minutes": "total_late_minutes", "leave_count": "leave_days",
                                  "on_duty_count": "on_duty_count", "hours_sum": "hours_sum",
                                  "hours_count": "hours_count"}.items()
        }}},
        {"$project": {"_id": 0, "department": "$_id", "headcount": 1, "present_days": 1,
                      "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1,
                      "avg_work_hours": {"$cond": [{"$gt": ["$hours_count", 0]},
                          round_half_up({"$divide": ["$hours_sum", "$hours_count"]}), None]}}},
        {"$sort": {"department": 1}},
    ]


def leaderboard_pipeline(month, limit=10, department=None):
    first, last = month_bounds(month)
    employee_match = {} if department is None else {"department": department}
    return [
        {"$match": {"date": {"$gte": first, "$lte": last}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "late_count": {"$sum": late_flag()}}},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code",
                     "pipeline": [{"$match": employee_match}, {"$project": {"_id": 0, "name": 1, "department": 1}}],
                     "as": "employee"}},
        {"$unwind": "$employee"},
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$project": {"_id": 0, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department",
                      "rank": 1, "total_late_minutes": 1, "late_count": 1}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ]


def validate_trend(first, last):
    if first > last or (Date.fromisoformat(last) - Date.fromisoformat(first)).days + 1 > 92:
        raise HTTPException(422, "Trend range must be ordered and contain at most 92 days")


def trend_pipeline(department, first, last):
    """Generate a complete calendar in MongoDB, even when there are zero logs.

    $range/$map is gap filling from a department employee anchor, rather than
    $densify over logs (which has no anchor for a completely empty range).
    """
    start = datetime.combine(Date.fromisoformat(first), time(), UTC)
    days = (Date.fromisoformat(last) - Date.fromisoformat(first)).days + 1
    return [
        {"$match": {"department": department}},
        {"$group": {"_id": None, "codes": {"$push": "$emp_code"}, "joins": {"$push": "$joined_on"}}},
        {"$set": {"day": {"$map": {"input": {"$range": [0, days]}, "as": "offset",
                                   "in": {"$dateAdd": {"startDate": start, "unit": "day", "amount": "$$offset"}}}}}},
        {"$unwind": "$day"},
        {"$set": {"date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d"}},
                  "is_working_day": is_weekday("$day")}},
        {"$set": {"headcount": {"$size": {"$filter": {"input": "$joins", "as": "joined",
                                                        "cond": {"$lte": ["$$joined", "$date"]}}}}}},
        {"$lookup": {"from": "attendance_logs", "let": {"date": "$date", "codes": "$codes"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$date", "$$date"]}, {"$in": ["$emp_code", "$$codes"]}]}}},
            {"$group": {"_id": None, "present_count": {"$sum": presence_weight(False)},
                        "late_count": {"$sum": late_flag()}}},
        ], "as": "stats"}},
        {"$set": {"present_count": {"$ifNull": [{"$arrayElemAt": ["$stats.present_count", 0]}, 0]},
                  "late_count": {"$ifNull": [{"$arrayElemAt": ["$stats.late_count", 0]}, 0]}}},
        {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                      round_half_up({"$divide": [{"$toDecimal": "$present_count"}, "$headcount"]}, 4), None]}}},
        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {
            "moving_avg_7d": {"$avg": {"$toDecimal": "$attendance_rate"}, "window": {"documents": [-6, 0]}},
        }}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1,
                      "late_count": 1, "attendance_rate": 1, "moving_avg_7d": round_half_up("$moving_avg_7d", 4)}},
        {"$sort": {"date": 1}},
    ]


def aggregate(collection, pipeline, hint):
    return json_value(list(db[collection].aggregate(pipeline, hint=hint, allowDiskUse=True)))


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: MonthString):
    emp = employee(emp_code)
    first, last = month_bounds(month)
    rows = aggregate("attendance_logs", monthly_pipeline(emp_code, month), "employee_date")
    stats = rows[0] if rows else {"present_days": 0, "leave_days": 0, "late_count": 0,
                                "total_late_minutes": 0, "total_overtime_minutes": 0}
    count = working_days(emp["joined_on"], first, last)
    percent = None if count == 0 else float((Decimal(str(stats["present_days"])) / Decimal(count) * 100).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP))
    return {"emp_code": emp_code, "month": month, "working_days": count, **stats, "attendance_pct": percent}


@app.get("/analytics/departments/summary")
def department_summary(month: MonthString, department: str | None = None):
    hint = "joined_department" if department is None else "department_code"
    return {"month": month, "items": aggregate("employees", summary_pipeline(month, department), hint)}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: MonthString, limit: Limit = 10, department: str | None = None):
    return {"month": month, "items": aggregate("attendance_logs", leaderboard_pipeline(month, limit, department), "date_employee")}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: Annotated[DateString, Query(alias="from")],
                     to_date: Annotated[DateString, Query(alias="to")]):
    validate_trend(from_date, to_date)
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "Department not found")
    return {"department": department,
            "items": aggregate("employees", trend_pipeline(department, from_date, to_date), "department_code")}


ExplainName = Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: ExplainName, emp_code: str | None = None, month: MonthString | None = None,
                     department: str | None = None, limit: Limit = 10,
                     date_from: DateString | None = None, date_to: DateString | None = None,
                     status: Status | None = None,
                     from_date: Annotated[DateString | None, Query(alias="from")] = None,
                     to_date: Annotated[DateString | None, Query(alias="to")] = None,
                     page: Page = 1, page_size: PageSize = 20):
    def require(**values):
        missing = [key for key, value in values.items() if value is None]
        if missing:
            raise HTTPException(422, "Missing required parameters: " + ", ".join(missing))

    if endpoint == "attendance_list":
        collection = "attendance_logs"
        query, hint = attendance_query(emp_code, date_from, date_to, status)
        command = {"find": collection, "filter": query, "projection": {"_id": 0},
                   "sort": {"date": -1, "emp_code": 1}, "skip": (page - 1) * page_size,
                   "limit": page_size, "hint": hint}
    else:
        if endpoint == "employee_monthly":
            require(emp_code=emp_code, month=month)
            employee(emp_code)
            collection, hint = "attendance_logs", "employee_date"
            pipeline = monthly_pipeline(emp_code, month)
        elif endpoint == "department_summary":
            require(month=month)
            collection, hint = "employees", "joined_department" if department is None else "department_code"
            pipeline = summary_pipeline(month, department)
        elif endpoint == "late_leaderboard":
            require(month=month)
            collection, hint = "attendance_logs", "date_employee"
            pipeline = leaderboard_pipeline(month, limit, department)
        else:
            require(department=department, **{"from": from_date, "to": to_date})
            validate_trend(from_date, to_date)
            if db.employees.find_one({"department": department}, {"_id": 1}) is None:
                raise HTTPException(404, "Department not found")
            collection, hint = "employees", "department_code"
            pipeline = trend_pipeline(department, from_date, to_date)
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}, "hint": hint, "allowDiskUse": True}
    result = db.command("explain", command, verbosity="executionStats")
    return {"endpoint": endpoint, "collection": collection, "explain": json_value(result)}
