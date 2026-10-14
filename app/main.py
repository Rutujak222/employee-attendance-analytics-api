"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import logging
import json
import os
from calendar import monthrange
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv
from bson import json_util
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

try:
    IST = ZoneInfo("Asia/Kolkata")
except ZoneInfoNotFoundError:
    # Windows may lack tzdata; the assignment defines IST as fixed UTC+05:30.
    IST = timezone(timedelta(hours=5, minutes=30), name="Asia/Kolkata")
    logging.getLogger(__name__).warning(
        "Asia/Kolkata timezone data unavailable; using fixed UTC+05:30."
    )

def setup_indexes():
    db.employees.create_index([("emp_code", 1)], unique=True)
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.employees.create_index([("department", 1), ("joined_on", 1), ("emp_code", 1)])
    db.employees.create_index([("joined_on", 1), ("department", 1), ("emp_code", 1)])
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])


@asynccontextmanager
async def lifespan(app):
    setup_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def normalize_utc(value: datetime) -> datetime:
    """Treat naive BSON datetimes as UTC and truncate instants to seconds."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(microsecond=0)


def epoch_millis_to_utc(value: int) -> datetime:
    return datetime.fromtimestamp(value // 1000, tz=timezone.utc)


def serialize_response(value):
    """Convert datetime leaves to epoch milliseconds without mutating documents."""
    if isinstance(value, datetime):
        elapsed = normalize_utc(value) - datetime(1970, 1, 1, tzinfo=timezone.utc)
        return (elapsed.days * 86400 + elapsed.seconds) * 1000
    if isinstance(value, dict):
        return {key: serialize_response(item) for key, item in value.items()}
    if isinstance(value, list):
        return [serialize_response(item) for item in value]
    return value


def query_validation_error(field, message):
    raise HTTPException(422, detail=[{
        "type": "value_error", "loc": ["query", field], "msg": message,
    }])


def validate_pagination(page, page_size):
    if page < 1:
        query_validation_error("page", "page must be at least 1")
    if not 1 <= page_size <= 100:
        query_validation_error("page_size", "page_size must be between 1 and 100")


def validate_query_date(field, value):
    if value is not None:
        try:
            if date.fromisoformat(value).isoformat() != value:
                raise ValueError("non-canonical date")
        except ValueError:
            query_validation_error(field, "Expected a valid calendar date in YYYY-MM-DD format")


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    local = normalize_utc(punch_in).astimezone(IST)
    day = local.date()
    if shift_end <= shift_start and local.time() < time.fromisoformat(shift_end):
        day -= timedelta(days=1)
    return day.isoformat()


def compute_late_minutes(
    punch_in: datetime, shift_start: str, shift_end: Optional[str] = None
) -> int:
    punch_in = normalize_utc(punch_in).astimezone(IST)
    h, m = map(int, shift_start.split(":"))
    day = (
        attendance_date(punch_in, shift_start, shift_end)
        if shift_end is not None else punch_in.date().isoformat()
    )
    start = datetime.fromisoformat(day).replace(hour=h, minute=m, tzinfo=IST)
    seconds = (punch_in - start).total_seconds()
    return int(seconds // 60) if seconds > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    punch_in = normalize_utc(punch_in)
    punch_out = normalize_utc(punch_out)
    elapsed = punch_out - punch_in
    microseconds = (
        (elapsed.days * 86400 + elapsed.seconds) * 1000000 + elapsed.microseconds
    )
    hours = Decimal(microseconds) / Decimal(3600000000)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(
    punch_out: datetime, shift_end: str, date_str: str, *, shift_start: str
) -> int:
    if shift_start is None:
        raise ValueError("shift_start is required to compute overtime")
    punch_out = normalize_utc(punch_out)
    h, m = map(int, shift_end.split(":"))
    end = datetime.fromisoformat(date_str).replace(hour=h, minute=m, tzinfo=IST)
    if shift_end <= shift_start:
        end += timedelta(days=1)
    minutes = int((punch_out - end).total_seconds() // 60)
    return minutes if minutes >= 30 else 0


def attendance_validation_error(field, message, location="body"):
    raise HTTPException(422, detail=[{
        "type": "value_error", "loc": [location, field], "msg": message,
    }])


def attendance_document(record):
    doc = dict(record)
    doc.pop("_id", None)
    for field, default in (
        ("punch_in", None), ("punch_out", None), ("work_hours", None),
        ("late_minutes", 0), ("overtime_minutes", 0), ("half_day", False), ("history", []),
    ):
        doc.setdefault(field, default)
    for field in ("punch_in", "punch_out"):
        if doc[field] is not None:
            doc[field] = normalize_utc(doc[field])
    return doc


def attendance_snapshot_filter(record):
    """Match the read state so concurrent closes/corrections cannot overwrite it."""
    query = {"emp_code": record["emp_code"], "date": record["date"]}
    for field in ("status", "punch_in", "punch_out", "work_hours", "late_minutes",
                  "overtime_minutes", "half_day", "history"):
        query[field] = {"$eq": record[field]} if field in record else {"$exists": False}
    return query


def validate_punch_interval(punch_in, punch_out):
    duration = punch_out - punch_in
    if duration <= timedelta(0) or duration > timedelta(hours=24):
        attendance_validation_error("punch_out", "punch_out must be after punch_in and within 24 hours")


def closed_attendance_values(punch_in, punch_out, employee, date_str):
    hours = compute_work_hours(punch_in, punch_out)
    return {
        "work_hours": hours,
        "overtime_minutes": compute_overtime(
            punch_out, employee["shift_end"], date_str, shift_start=employee["shift_start"]),
        "half_day": hours < 4.50,
    }


def month_bounds(month):
    if not isinstance(month, str) or len(month) != 7:
        query_validation_error("month", "Expected a calendar month in YYYY-MM format")
    validate_query_date("month", month + "-01")
    start = date.fromisoformat(month + "-01")
    end = start.replace(day=monthrange(start.year, start.month)[1])
    return start.isoformat(), end.isoformat()


def trend_bounds(from_, to):
    for field, value in (("from", from_), ("to", to)):
        if value is None:
            query_validation_error(field, "Field required")
        validate_query_date(field, value)
    days = (date.fromisoformat(to) - date.fromisoformat(from_)).days + 1
    if not 1 <= days <= 92:
        query_validation_error("to", "Range must contain between 1 and 92 calendar days")
    return days


def mongo_calendar_date(value):
    return {"$dateFromString": {"dateString": value, "format": "%Y-%m-%d", "timezone": "Asia/Kolkata"}}


def mongo_working_day(value):
    return {"$in": [{"$dayOfWeek": {"date": value, "timezone": "Asia/Kolkata"}}, [2, 3, 4, 5, 6]]}


def mongo_presence():
    return {"$cond": [{"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                      {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}


def mongo_half_up(value, places=2):
    # Reported analytics are non-negative; floor(x * scale + 0.5) is half-up.
    scale = 10 ** places
    return {"$cond": [{"$eq": [value, None]}, None, {"$divide": [
        {"$floor": {"$add": [{"$multiply": [{"$toDecimal": value}, scale]}, {"$toDecimal": "0.5"}]}},
        scale,
    ]}]}


def monthly_metrics_group():
    present = {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}
    worked = {"$and": [present, {"$ne": [{"$ifNull": ["$work_hours", None]}, None]}]}
    late = {"$ifNull": ["$late_minutes", 0]}
    return {"$group": {
        "_id": None,
        "present_days": {"$sum": {"$cond": [mongo_working_day(mongo_calendar_date("$date")), mongo_presence(), 0]}},
        "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
        "late_count": {"$sum": {"$cond": [{"$gt": [late, 0]}, 1, 0]}},
        "total_late_minutes": {"$sum": late},
        "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
        "work_total": {"$sum": {"$cond": [worked, {"$toDecimal": "$work_hours"}, 0]}},
        "work_count": {"$sum": {"$cond": [worked, 1, 0]}},
    }}


def monthly_logs_lookup(start, end):
    return {"$lookup": {
        "from": "attendance_logs", "let": {"code": "$emp_code"}, "as": "stats",
        "pipeline": [
            {"$match": {"date": {"$gte": start, "$lte": end}, "$expr": {"$eq": ["$emp_code", "$$code"]}}},
            monthly_metrics_group(),
        ],
    }}


def aggregate_operation(collection, pipeline, hint):
    return {"aggregate": collection, "pipeline": pipeline, "cursor": {}, "hint": dict(hint), "allowDiskUse": True}


def build_employee_monthly(emp_code, month):
    if emp_code is None:
        query_validation_error("emp_code", "Field required")
    start, end = month_bounds(month)
    days = date.fromisoformat(end).day
    calendar_day = {"$dateAdd": {"startDate": mongo_calendar_date(start), "unit": "day",
                                 "amount": "$$offset", "timezone": "Asia/Kolkata"}}
    day_string = {"$dateToString": {"date": calendar_day, "format": "%Y-%m-%d", "timezone": "Asia/Kolkata"}}
    pipeline = [
        {"$match": {"emp_code": emp_code}},
        monthly_logs_lookup(start, end),
        {"$set": {"stats": {"$arrayElemAt": ["$stats", 0]}, "working_days": {"$size": {"$filter": {
            "input": {"$range": [0, days]}, "as": "offset", "cond": {"$and": [
                {"$gte": [day_string, "$joined_on"]}, mongo_working_day(calendar_day),
            ]},
        }}}}},
        {"$project": {"_id": 0, "emp_code": 1, "month": {"$literal": month}, "working_days": 1,
            **{field: {"$ifNull": ["$stats." + field, 0]} for field in (
                "present_days", "leave_days", "late_count", "total_late_minutes", "total_overtime_minutes")}}},
        {"$set": {"present_days": mongo_half_up("$present_days"), "attendance_pct": {"$cond": [
            {"$gt": ["$working_days", 0]}, mongo_half_up({"$multiply": [
                {"$divide": [{"$toDecimal": "$present_days"}, "$working_days"]}, 100]}), None,
        ]}}},
    ]
    return aggregate_operation("employees", pipeline, [("emp_code", 1)])


def build_department_summary(month, department=None):
    start, end = month_bounds(month)
    query = {"joined_on": {"$lte": end}}
    if department is not None:
        query["department"] = department
    group = {"_id": "$department", "headcount": {"$sum": 1}}
    for field in ("present_days", "late_count", "total_late_minutes", "on_duty_count", "work_total", "work_count"):
        group[field] = {"$sum": {"$ifNull": ["$stats." + field, 0]}}
    group["leave_count"] = {"$sum": {"$ifNull": ["$stats.leave_days", 0]}}
    pipeline = [
        {"$match": query}, monthly_logs_lookup(start, end),
        {"$set": {"stats": {"$arrayElemAt": ["$stats", 0]}}}, {"$group": group},
        {"$project": {"_id": 0, "department": "$_id", "headcount": 1,
            "present_days": mongo_half_up("$present_days"), "late_count": 1, "total_late_minutes": 1,
            "leave_count": 1, "on_duty_count": 1, "avg_work_hours": {"$cond": [
                {"$gt": ["$work_count", 0]}, mongo_half_up({"$divide": ["$work_total", "$work_count"]}), None,
            ]}}}, {"$sort": {"department": 1}},
    ]
    hint = ([("department", 1), ("joined_on", 1), ("emp_code", 1)] if department is not None
            else [("joined_on", 1), ("department", 1), ("emp_code", 1)])
    return aggregate_operation("employees", pipeline, hint)


def build_late_leaderboard(month, limit=10, department=None):
    start, end = month_bounds(month)
    if not 1 <= limit <= 50:
        query_validation_error("limit", "limit must be between 1 and 50")
    pipeline = [
        {"$match": {"date": {"$gte": start, "$lte": end}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend([
        {"$project": {"_id": 0, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department",
                      "total_late_minutes": 1, "late_count": 1}},
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}}, {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ])
    return aggregate_operation("attendance_logs", pipeline, [("date", -1), ("emp_code", 1)])


def build_department_trend(department, from_, to):
    if department is None:
        query_validation_error("department", "Field required")
    days = trend_bounds(from_, to)
    pipeline = [
        {"$match": {"department": department}},
        {"$group": {"_id": None, "employees": {"$push": {"emp_code": "$emp_code", "joined_on": "$joined_on"}}}},
        {"$project": {"_id": 0, "employees": 1, "offset": {"$range": [0, days]}}}, {"$unwind": "$offset"},
        {"$set": {"day": {"$dateAdd": {"startDate": mongo_calendar_date(from_), "unit": "day",
                                       "amount": "$offset", "timezone": "Asia/Kolkata"}}}},
        {"$set": {"date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d", "timezone": "Asia/Kolkata"}},
                  "is_working_day": mongo_working_day("$day")}},
        {"$set": {"headcount": {"$size": {"$filter": {"input": "$employees", "as": "employee",
                                                       "cond": {"$lte": ["$$employee.joined_on", "$date"]}}}}}},
        {"$lookup": {"from": "attendance_logs", "as": "stats", "let": {
            "day": "$date", "codes": {"$map": {"input": "$employees", "as": "employee", "in": "$$employee.emp_code"}}},
            "pipeline": [
                {"$match": {"$expr": {"$and": [{"$eq": ["$date", "$$day"]}, {"$in": ["$emp_code", "$$codes"]}]}}},
                {"$group": {"_id": None, "present_count": {"$sum": mongo_presence()},
                            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
            ]}},
        {"$set": {"stats": {"$arrayElemAt": ["$stats", 0]}}},
        {"$set": {"present_count": {"$ifNull": ["$stats.present_count", 0]},
                  "late_count": {"$ifNull": ["$stats.late_count", 0]}}},
        {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            mongo_half_up({"$divide": [{"$toDecimal": "$present_count"}, "$headcount"]}, 4), None]}}},
        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {
            "moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1,
                      "present_count": mongo_half_up("$present_count"), "late_count": 1,
                      "attendance_rate": 1, "moving_avg_7d": mongo_half_up("$moving_avg_7d", 4)}},
        {"$sort": {"date": 1}},
    ]
    return aggregate_operation("employees", pipeline, [("department", 1), ("joined_on", 1), ("emp_code", 1)])


def analytics_json(value):
    if isinstance(value, dict):
        return {key: analytics_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [analytics_json(item) for item in value]
    if hasattr(value, "to_decimal"):
        return float(value.to_decimal())
    return value


def run_analytics(operation):
    return analytics_json(list(getattr(db, operation["aggregate"]).aggregate(
        operation["pipeline"], hint=operation["hint"], allowDiskUse=operation["allowDiskUse"])))


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value):
        datetime.strptime(value, "%Y-%m-%d")
        return value


class PunchInIn(BaseModel):
    emp_code: str
    # The omitted default is internal; an explicitly supplied null is invalid.
    punched_at: int = Field(default=None, strict=True, ge=100000000000, le=4102444800000)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: int = Field(default=None, strict=True, ge=100000000000, le=4102444800000)


class RegularizeIn(BaseModel):
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    status: Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"] = None
    punch_in: int = Field(default=None, strict=True, ge=100000000000, le=4102444800000)
    punch_out: int = Field(default=None, strict=True, ge=100000000000, le=4102444800000)


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        db.command("ping")
    except PyMongoError:
        raise HTTPException(503, "MongoDB unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if body.shift_start == body.shift_end:
        raise HTTPException(422, detail=[{
            "type": "value_error", "loc": ["body", "shift_end"],
            "msg": "Value error, shift_start must differ from shift_end",
        }])
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(409, "emp_code already exists")
    doc = body.model_dump()
    doc["created_at"] = normalize_utc(datetime.now(timezone.utc))
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    doc.pop("_id", None)
    return serialize_response(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = 1, page_size: int = 20):
    validate_pagination(page, page_size)
    q = {}
    if department is not None:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = list(db.employees.find(q, {"_id": 0}).sort([("emp_code", 1)]).skip(skip).limit(page_size))
    return serialize_response({"items": items, "total": total, "page": page, "page_size": page_size})


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = (
        epoch_millis_to_utc(body.punched_at)
        if body.punched_at is not None
        else normalize_utc(datetime.now(timezone.utc))
    )
    d = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    if db.attendance_logs.find_one({"emp_code": body.emp_code, "date": d}):
        raise HTTPException(409, "already punched in for this date")
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], emp["shift_end"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    doc.pop("_id", None)
    return serialize_response(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = (epoch_millis_to_utc(body.punched_at) if body.punched_at is not None
          else normalize_utc(datetime.now(timezone.utc)))
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$type": "date", "$lte": ts}},
        sort=[("punch_in", -1)],
    )
    if record is None:
        raise HTTPException(404, "no punch-in found")
    doc = attendance_document(record)
    if doc["punch_out"] is not None:
        raise HTTPException(409, "already punched out")
    validate_punch_interval(doc["punch_in"], ts)
    values = {"punch_in": doc["punch_in"], "punch_out": ts}
    values.update(closed_attendance_values(doc["punch_in"], ts, emp, doc["date"]))
    result = db.attendance_logs.update_one(attendance_snapshot_filter(record), {"$set": values})
    if result.matched_count != 1:
        raise HTTPException(409, "attendance changed concurrently")
    doc.update(values)
    return serialize_response(doc)


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    # Keep calendar values as strings while rejecting malformed path dates.
    try:
        parsed = datetime.strptime(date, "%Y-%m-%d")
        if f"{parsed.year:04d}-{parsed.month:02d}-{parsed.day:02d}" != date:
            raise ValueError("non-canonical date")
    except ValueError:
        attendance_validation_error("date", "Expected a valid calendar date in YYYY-MM-DD format", "path")
    supplied = body.model_dump(exclude_unset=True)
    if supplied.get("status") in ("ABSENT", "LEAVE") and any(
        field in supplied for field in ("punch_in", "punch_out")
    ):
        attendance_validation_error("status", "ABSENT or LEAVE cannot include punch times")
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if record is None:
        raise HTTPException(404, "attendance record not found")
    before = attendance_document(record)
    doc = dict(before)
    if "status" in supplied:
        doc["status"] = supplied["status"]
    for field in ("punch_in", "punch_out"):
        if field in supplied:
            doc[field] = epoch_millis_to_utc(supplied[field])
    if doc["status"] in ("ABSENT", "LEAVE"):
        if any(field in supplied for field in ("punch_in", "punch_out")):
            attendance_validation_error("status", "ABSENT or LEAVE cannot include punch times")
        doc.update(punch_in=None, punch_out=None, work_hours=None,
                   late_minutes=0, overtime_minutes=0, half_day=False)
    else:
        if doc["punch_in"] is None:
            attendance_validation_error("punch_in", "A presence status requires punch_in")
        if attendance_date(doc["punch_in"], emp["shift_start"], emp["shift_end"]) != date:
            attendance_validation_error("punch_in", "punch_in must stay on the record's attendance date")
        doc["late_minutes"] = compute_late_minutes(doc["punch_in"], emp["shift_start"], emp["shift_end"])
        if doc["punch_out"] is None:
            doc.update(work_hours=None, overtime_minutes=0, half_day=False)
        else:
            validate_punch_interval(doc["punch_in"], doc["punch_out"])
            doc.update(closed_attendance_values(doc["punch_in"], doc["punch_out"], emp, date))
    if all(doc[field] == before[field] for field in ("status", "punch_in", "punch_out")):
        attendance_validation_error("body", "Correction changes nothing")
    fields = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")
    changes = {field: {"from": before[field], "to": doc[field]}
               for field in fields if before[field] != doc[field]}
    entry = {"at": normalize_utc(datetime.now(timezone.utc)), "by": body.regularized_by,
             "reason": body.reason, "changes": changes}
    values = {field: doc[field] for field in fields}
    result = db.attendance_logs.update_one(
        attendance_snapshot_filter(record), {"$set": values, "$push": {"history": entry}})
    if result.matched_count != 1:
        raise HTTPException(409, "attendance changed concurrently")
    doc["history"] = before["history"] + [entry]
    return serialize_response(doc)


def build_attendance_list(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    validate_pagination(page, page_size)
    validate_query_date("date_from", date_from)
    validate_query_date("date_to", date_to)
    if date_from is not None and date_to is not None and date_from > date_to:
        query_validation_error("date_to", "date_to must be on or after date_from")
    if status is not None and status not in ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"):
        query_validation_error("status", "Invalid attendance status")
    q = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        q["date"] = {}
        if date_from is not None:
            q["date"]["$gte"] = date_from
        if date_to is not None:
            q["date"]["$lte"] = date_to
    if status is not None:
        q["status"] = status
    return {"find": "attendance_logs", "filter": q, "projection": {"_id": 0},
            "sort": {"date": -1, "emp_code": 1}, "hint": {"date": -1, "emp_code": 1},
            "skip": (page - 1) * page_size, "limit": page_size}


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    operation = build_attendance_list(emp_code, date_from, date_to, status, page, page_size)
    total = db.attendance_logs.count_documents(operation["filter"])
    cursor = db.attendance_logs.find(operation["filter"], operation["projection"], hint=operation["hint"]).sort(
        list(operation["sort"].items()))
    page_docs = [dict(doc) for doc in cursor.skip(operation["skip"]).limit(operation["limit"])]
    for d in page_docs:
        d.pop("_id", None)
        for field, default in (
            ("punch_in", None), ("punch_out", None), ("work_hours", None),
            ("late_minutes", 0), ("overtime_minutes", 0), ("half_day", False), ("history", []),
        ):
            d.setdefault(field, default)
    return serialize_response({"items": page_docs, "total": total, "page": page, "page_size": page_size})


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    items = run_analytics(build_employee_monthly(emp_code, month))
    if not items:
        raise HTTPException(404, "employee not found")
    return items[0]


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    return {"month": month, "items": run_analytics(build_department_summary(month, department))}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = 10, department: Optional[str] = None):
    return {"month": month, "items": run_analytics(build_late_leaderboard(month, limit, department))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: Annotated[str, Query(alias="from")], to: str):
    items = run_analytics(build_department_trend(department, from_, to))
    if not items:
        raise HTTPException(404, "department not found")
    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str, emp_code: Optional[str] = None, month: Optional[str] = None,
    department: Optional[str] = None, limit: int = 10,
    date_from: Optional[str] = None, date_to: Optional[str] = None, status: Optional[str] = None,
    from_: Annotated[Optional[str], Query(alias="from")] = None, to: Optional[str] = None,
    page: int = 1, page_size: int = 20,
):
    names = ("attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend")
    if endpoint not in names:
        attendance_validation_error("endpoint", "Invalid endpoint name", "path")
    # Validate all supplied parameters declared by the admin contract, even if unused.
    listing = build_attendance_list(emp_code, date_from, date_to, status, page, page_size)
    if month is not None:
        month_bounds(month)
    if not 1 <= limit <= 50:
        query_validation_error("limit", "limit must be between 1 and 50")
    validate_query_date("from", from_)
    validate_query_date("to", to)
    if endpoint == "attendance_list":
        operation = listing
    elif endpoint == "employee_monthly":
        operation = build_employee_monthly(emp_code, month)
    elif endpoint == "department_summary":
        operation = build_department_summary(month, department)
    elif endpoint == "late_leaderboard":
        operation = build_late_leaderboard(month, limit, department)
    else:
        operation = build_department_trend(department, from_, to)
    stats = db.command("explain", operation, verbosity="executionStats")
    # Explain can contain BSON values; preserve its structure as MongoDB Extended JSON.
    return {"endpoint": endpoint, "collection": operation.get("aggregate", operation.get("find")),
            "explain": json.loads(json_util.dumps(stats))}
