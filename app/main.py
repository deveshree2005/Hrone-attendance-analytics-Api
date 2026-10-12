
"""
HROne Employee Attendance & Analytics API
Run from the project root:
    uvicorn app.main:app --port 8000
"""

import os
import re
import calendar
from datetime import datetime, date, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from pydantic import BaseModel, Field, ConfigDict, StrictInt, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError

# Existing environment variables take precedence over .env.
load_dotenv(override=False)

MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB = os.getenv("MONGO_DB")

client = None
db = None

if MONGO_URI and MONGO_DB:
    client = MongoClient(
        MONGO_URI,
        serverSelectionTimeoutMS=3000,
        tz_aware=True,
        tzinfo=timezone.utc,
    )
    db = client[MONGO_DB]

app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
)

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc

PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
STATUSES = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}

EPOCH_MIN = 100_000_000_000
EPOCH_MAX = 4_102_444_800_000


# =========================================================
# Validation models
# =========================================================

def validate_epoch(value):
    if value is None:
        return None

    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("Must be an integer epoch-milliseconds value")

    if not EPOCH_MIN <= value <= EPOCH_MAX:
        raise ValueError("Epoch milliseconds are outside the allowed range")

    return value


class EmployeeCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(
        max_length=120,
        pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
    )
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(
        default="09:30",
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$",
    )
    shift_end: str = Field(
        default="18:30",
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$",
    )
    joined_on: date

    @model_validator(mode="after")
    def check_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("Shift start and end must differ")
        return self


class PunchInRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: str = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def check_time(cls, value):
        return validate_epoch(value)

    @field_validator("status")
    @classmethod
    def check_status(cls, value):
        if value not in PRESENCE:
            raise ValueError("Use PRESENT, WFH, or ON_DUTY")
        return value


class PunchOutRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: Optional[StrictInt] = None

    @field_validator("punched_at")
    @classmethod
    def check_time(cls, value):
        return validate_epoch(value)


class RegularizeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: Optional[str] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out")
    @classmethod
    def check_time(cls, value):
        return validate_epoch(value)

    @field_validator("status")
    @classmethod
    def check_status(cls, value):
        if value is not None and value not in STATUSES:
            raise ValueError("Invalid attendance status")
        return value


# =========================================================
# Shared helpers
# =========================================================

def require_db():
    if db is None:
        raise HTTPException(503, "MongoDB is not configured")
    return db


def now_utc():
    return datetime.now(UTC).replace(microsecond=0)


def from_epoch(value):
    """Convert epoch milliseconds to a whole-second UTC datetime."""
    if value is None:
        return None
    return datetime.fromtimestamp(value // 1000, tz=UTC)


def to_epoch(value):
    """Convert a MongoDB datetime to epoch milliseconds."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.astimezone(UTC).timestamp()) * 1000


def rounded(value, places=2):
    quantum = Decimal("1").scaleb(-places)
    return float(
        Decimal(str(value)).quantize(
            quantum,
            rounding=ROUND_HALF_UP,
        )
    )


def public_employee(doc):
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": to_epoch(doc["created_at"]),
    }


def public_attendance(doc):
    history = []

    for entry in doc.get("history", []):
        changes = {}

        for field, values in entry.get("changes", {}).items():
            before = values.get("from")
            after = values.get("to")

            if field in ("punch_in", "punch_out"):
                before = to_epoch(before)
                after = to_epoch(after)

            changes[field] = {"from": before, "to": after}

        history.append({
            "at": to_epoch(entry["at"]),
            "by": entry["by"],
            "reason": entry["reason"],
            "changes": changes,
        })

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_epoch(doc.get("punch_in")),
        "punch_out": to_epoch(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": int(doc.get("late_minutes", 0) or 0),
        "overtime_minutes": int(doc.get("overtime_minutes", 0) or 0),
        "half_day": bool(doc.get("half_day", False)),
        "history": history,
    }


def local_shift_time(day, shift):
    hour, minute = map(int, shift.split(":"))
    return datetime.combine(
        day,
        time(hour, minute),
        tzinfo=IST,
    )


def get_attendance_date(punch_in, employee):
    local = punch_in.astimezone(IST)

    if (
        employee["shift_end"] <= employee["shift_start"]
        and local.strftime("%H:%M") < employee["shift_end"]
    ):
        return local.date() - timedelta(days=1)

    return local.date()


def get_shift_bounds(record_date, employee):
    start = local_shift_time(record_date, employee["shift_start"])

    end_date = record_date
    if employee["shift_end"] <= employee["shift_start"]:
        end_date += timedelta(days=1)

    end = local_shift_time(end_date, employee["shift_end"])

    return start.astimezone(UTC), end.astimezone(UTC)


def calculate_derived(status, punch_in, punch_out, record_date, employee):
    if status not in PRESENCE:
        return {
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
        }

    shift_start, shift_end = get_shift_bounds(record_date, employee)

    late_minutes = 0
    overtime_minutes = 0
    work_hours = None
    half_day = False

    if punch_in is not None:
        elapsed = (punch_in - shift_start).total_seconds()

        # Strictly more than ten minutes late.
        if elapsed > 600:
            late_minutes = int(elapsed // 60)

    if punch_in is not None and punch_out is not None:
        seconds = (punch_out - punch_in).total_seconds()
        work_hours = rounded(
            Decimal(str(seconds)) / Decimal("3600"),
            2,
        )

        overtime_seconds = (punch_out - shift_end).total_seconds()

        if overtime_seconds >= 1800:
            overtime_minutes = int(overtime_seconds // 60)

        half_day = work_hours < 4.50

    return {
        "work_hours": work_hours,
        "late_minutes": late_minutes,
        "overtime_minutes": overtime_minutes,
        "half_day": half_day,
    }


def month_dates(month):
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise HTTPException(422, "month must use YYYY-MM")

    year, month_number = map(int, month.split("-"))

    first = date(year, month_number, 1)
    last = date(
        year,
        month_number,
        calendar.monthrange(year, month_number)[1],
    )

    return first, last


def working_days(start, end):
    if start > end:
        return 0

    total = 0
    current = start

    while current <= end:
        if current.weekday() < 5:
            total += 1
        current += timedelta(days=1)

    return total


def db_indexes():
    d = require_db()

    d.employees.create_index(
        [("emp_code", ASCENDING)],
        unique=True,
        name="uq_employee_code",
    )
    d.employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING)],
        name="ix_employee_department_joined",
    )

    d.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="uq_attendance_employee_date",
    )
    d.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="ix_attendance_date_employee",
    )
    d.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)],
        name="ix_attendance_employee_date_status",
    )
    d.attendance_logs.create_index(
        [("date", ASCENDING), ("late_minutes", DESCENDING), ("emp_code", ASCENDING)],
        name="ix_attendance_date_late",
    )


@app.on_event("startup")
def startup():
    if db is not None:
        try:
            db.command("ping")
            db_indexes()
        except Exception:
            # The readiness endpoint will report MongoDB failures.
            pass


@app.get("/health", tags=["system"])
def health():
    if db is None:
        raise HTTPException(503, "MongoDB is not configured")

    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "MongoDB is unavailable")


# =========================================================
# Employee endpoints
# =========================================================

@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeCreate):
    d = require_db()

    doc = body.model_dump()
    doc["joined_on"] = body.joined_on.isoformat()
    doc["created_at"] = now_utc()

    try:
        d.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")

    return public_employee(doc)


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    d = require_db()

    query = {}
    if department is not None:
        query["department"] = department

    # Both the count and the page use the same filters.
    total = d.employees.count_documents(query)

    documents = (
        d.employees.find(query, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    return {
        "items": [public_employee(x) for x in documents],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# =========================================================
# Punch-in and punch-out
# =========================================================

@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(body: PunchInRequest):
    d = require_db()

    employee = d.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")

    instant = from_epoch(body.punched_at) if body.punched_at is not None else now_utc()
    record_date = get_attendance_date(instant, employee)

    derived = calculate_derived(
        body.status,
        instant,
        None,
        record_date,
        employee,
    )

    doc = {
        "emp_code": body.emp_code,
        "date": record_date.isoformat(),
        "status": body.status,
        "punch_in": instant,
        "punch_out": None,
        **derived,
        "history": [],
    }

    try:
        d.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this attendance date")

    return public_attendance(doc)


@app.post("/attendance/punch-out", tags=["attendance"])
def punch_out(body: PunchOutRequest):
    d = require_db()

    employee = d.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")

    instant = from_epoch(body.punched_at) if body.punched_at is not None else now_utc()

    record = d.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "status": {"$in": list(PRESENCE)},
            "punch_in": {"$ne": None, "$lte": instant},
        },
        sort=[("punch_in", DESCENDING)],
    )

    if record is None:
        raise HTTPException(404, "no punch-in found")

    if record.get("punch_out") is not None:
        raise HTTPException(409, "record is already punched out")

    if (
        instant <= record["punch_in"]
        or instant > record["punch_in"] + timedelta(hours=24)
    ):
        raise HTTPException(
            422,
            "punch-out must be after punch-in and within 24 hours",
        )

    derived = calculate_derived(
        record["status"],
        record["punch_in"],
        instant,
        date.fromisoformat(record["date"]),
        employee,
    )

    updated = d.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": instant,
                **derived,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if updated is None:
        raise HTTPException(409, "record was updated concurrently")

    return public_attendance(updated)


# =========================================================
# Attendance list and manual correction
# =========================================================

@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    d = require_db()

    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")

    if status is not None and status not in STATUSES:
        raise HTTPException(422, "invalid status")

    query = {}

    if emp_code is not None:
        query["emp_code"] = emp_code

    if date_from is not None or date_to is not None:
        query["date"] = {}

        if date_from is not None:
            query["date"]["$gte"] = date_from.isoformat()

        if date_to is not None:
            query["date"]["$lte"] = date_to.isoformat()

    if status is not None:
        query["status"] = status

    total = d.attendance_logs.count_documents(query)

    documents = (
        d.attendance_logs.find(query)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    return {
        "items": [public_attendance(x) for x in documents],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(
    emp_code: str,
    date: date = Path(...),
    body: RegularizeRequest = ...,
):
    d = require_db()

    employee = d.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")

    record = d.attendance_logs.find_one({
        "emp_code": emp_code,
        "date": date.isoformat(),
    })

    if record is None:
        raise HTTPException(404, "attendance record not found")

    supplied = body.model_fields_set - {"reason", "regularized_by"}

    if not supplied:
        raise HTTPException(422, "request must change at least one field")

    new_status = (
        body.status
        if "status" in supplied and body.status is not None
        else record["status"]
    )

    new_in = (
        from_epoch(body.punch_in)
        if "punch_in" in supplied
        else record.get("punch_in")
    )

    new_out = (
        from_epoch(body.punch_out)
        if "punch_out" in supplied
        else record.get("punch_out")
    )

    if new_status in {"ABSENT", "LEAVE"}:
        if (
            ("punch_in" in supplied and body.punch_in is not None)
            or ("punch_out" in supplied and body.punch_out is not None)
        ):
            raise HTTPException(422, "ABSENT and LEAVE cannot have punch times")

        new_in = None
        new_out = None

    elif new_in is None:
        raise HTTPException(422, "presence status requires punch_in")

    if new_in is not None:
        actual_date = get_attendance_date(new_in, employee)

        if actual_date != date:
            raise HTTPException(422, "punch_in must belong to the attendance date")

    if new_out is not None:
        if (
            new_in is None
            or new_out <= new_in
            or new_out > new_in + timedelta(hours=24)
        ):
            raise HTTPException(422, "invalid punch_out")

    derived = calculate_derived(
        new_status,
        new_in,
        new_out,
        date,
        employee,
    )

    candidate = {
        "status": new_status,
        "punch_in": new_in,
        "punch_out": new_out,
        **derived,
    }

    old_values = {
        "status": record.get("status"),
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
        "work_hours": record.get("work_hours"),
        "late_minutes": int(record.get("late_minutes", 0) or 0),
        "overtime_minutes": int(record.get("overtime_minutes", 0) or 0),
        "half_day": bool(record.get("half_day", False)),
    }

    changes = {}

    for field, value in candidate.items():
        if old_values.get(field) != value:
            changes[field] = {
                "from": old_values.get(field),
                "to": value,
            }

    if not changes:
        raise HTTPException(422, "request does not change the record")

    history_entry = {
        "at": now_utc(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Compare-and-swap: concurrent edits cannot silently overwrite history.
    query = {
        "_id": record["_id"],
        "history": record.get("history", []),
        "status": record["status"],
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
    }

    updated = d.attendance_logs.find_one_and_update(
        query,
        {
            "$set": candidate,
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if updated is None:
        raise HTTPException(409, "record changed concurrently; retry")

    return public_attendance(updated)


# =========================================================
# Employee monthly analytics
# =========================================================

@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    d = require_db()

    employee = d.employees.find_one({"emp_code": emp_code})

    if employee is None:
        raise HTTPException(404, "employee not found")

    first, last = month_dates(month)

    joined = date.fromisoformat(employee["joined_on"])
    effective_start = max(first, joined)

    total_working_days = working_days(effective_start, last)

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {
                    "$gte": first.isoformat(),
                    "$lte": last.isoformat(),
                },
            }
        },
        {
            "$set": {
                "weekday": {
                    "$dayOfWeek": {
                        "$dateFromString": {"dateString": "$date"}
                    }
                }
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", list(PRESENCE)]},
                                    {"$gte": ["$weekday", 2]},
                                    {"$lte": ["$weekday", 6]},
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": [{"$ifNull": ["$half_day", False]}, True]},
                                    0.5,
                                    1.0,
                                ]
                            },
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
    ]

    results = list(d.attendance_logs.aggregate(pipeline))
    result = results[0] if results else {}

    present = rounded(result.get("present_days", 0), 2)

    attendance_pct = (
        rounded(Decimal(str(present)) / Decimal(total_working_days) * 100, 4)
        if total_working_days
        else None
    )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": total_working_days,
        "present_days": present,
        "leave_days": result.get("leave_days", 0),
        "late_count": result.get("late_count", 0),
        "total_late_minutes": result.get("total_late_minutes", 0),
        "total_overtime_minutes": result.get("total_overtime_minutes", 0),
        "attendance_pct": attendance_pct,
    }


# =========================================================
# Department summary
# =========================================================

@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    d = require_db()
    first, last = month_dates(month)

    employee_match = {
        "joined_on": {"$lte": last.isoformat()}
    }

    if department is not None:
        employee_match["department"] = department

    pipeline = [
        {"$match": employee_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$gte": ["$date", first.isoformat()]},
                                    {"$lte": ["$date", last.isoformat()]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$set": {
                "weekday": {
                    "$cond": [
                        {"$ne": [{"$ifNull": ["$logs.date", None]}, None]},
                        {"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}},
                        0,
                    ]
                }
            }
        },
        {
            "$group": {
                "_id": "$department",
                "employee_codes": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", list(PRESENCE)]},
                                    {"$gte": ["$weekday", 2]},
                                    {"$lte": ["$weekday", 6]},
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": [{"$ifNull": ["$logs.half_day", False]}, True]},
                                    0.5,
                                    1.0,
                                ]
                            },
                            0.0,
                        ]
                    }
                },
                "hours": {
                    "$push": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", list(PRESENCE)]},
                                    {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]},
                                ]
                            },
                            "$logs.work_hours",
                            None,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}
                },
                "on_duty_count": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$employee_codes"},
                "present_days": {"$round": ["$present_days", 2]},
                "avg_work_hours": {
                    "$let": {
                        "vars": {
                            "valid": {
                                "$filter": {
                                    "input": "$hours",
                                    "as": "h",
                                    "cond": {"$ne": ["$$h", None]},
                                }
                            }
                        },
                        "in": {
                            "$cond": [
                                {"$gt": [{"$size": "$$valid"}, 0]},
                                {"$round": [{"$avg": "$$valid"}, 2]},
                                None,
                            ]
                        },
                    }
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$match": {"headcount": {"$gt": 0}}},
        {"$sort": {"department": 1}},
    ]

    items = list(d.employees.aggregate(pipeline, allowDiskUse=True))
    return {"month": month, "items": items}


# =========================================================
# Late leaderboard
# =========================================================

@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    d = require_db()
    first, last = month_dates(month)

    pipeline = [
        {
            "$match": {
                "date": {
                    "$gte": first.isoformat(),
                    "$lte": last.isoformat(),
                },
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]

    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ])

    items = list(d.attendance_logs.aggregate(pipeline, allowDiskUse=True))
    return {"month": month, "items": items}


# =========================================================
# Department trend
# =========================================================

@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str,
    from_date: date = Query(..., alias="from"),
    to_date: date = Query(..., alias="to"),
):
    d = require_db()

    if to_date < from_date:
        raise HTTPException(422, "'to' must not be before 'from'")

    day_count = (to_date - from_date).days + 1

    if day_count > 92:
        raise HTTPException(422, "date range cannot exceed 92 days")

    if d.employees.count_documents({"department": department}) == 0:
        raise HTTPException(404, "department not found")

    # MongoDB generates every calendar day in the requested range.
    # No Python loop is used to build the trend rows.
    pipeline = [
        {"$documents": [{"start": datetime.combine(from_date, time.min, tzinfo=UTC)}]},
        {
            "$project": {
                "days": {
                    "$map": {
                        "input": {"$range": [0, day_count]},
                        "as": "offset",
                        "in": {
                            "$dateAdd": {
                                "startDate": "$start",
                                "unit": "day",
                                "amount": "$$offset",
                            }
                        },
                    }
                }
            }
        },
        {"$unwind": "$days"},
        {
            "$set": {
                "date": {
                    "$dateToString": {
                        "date": "$days",
                        "format": "%Y-%m-%d",
                        "timezone": "UTC",
                    }
                },
                "is_working_day": {
                    "$and": [
                        {"$gte": [{"$dayOfWeek": "$days"}, 2]},
                        {"$lte": [{"$dayOfWeek": "$days"}, 6]},
                    ]
                },
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"day": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$day"]},
                                ]
                            }
                        }
                    },
                    {"$count": "n"},
                ],
                "as": "hc",
            }
        },
        {
            "$set": {
                "headcount": {
                    "$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]
                }
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"day": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {"$eq": ["$date", "$$day"]}
                        }
                    },
                    {
                        "$lookup": {
                            "from": "employees",
                            "localField": "emp_code",
                            "foreignField": "emp_code",
                            "as": "employee",
                        }
                    },
                    {"$unwind": "$employee"},
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$employee.department", department]},
                                    {"$lte": ["$employee.joined_on", "$$day"]},
                                ]
                            }
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {
            "$set": {
                "present_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {
                                "$cond": [
                                    {"$in": ["$$log.status", list(PRESENCE)]},
                                    {
                                        "$cond": [
                                            {"$eq": [{"$ifNull": ["$$log.half_day", False]}, True]},
                                            0.5,
                                            1.0,
                                        ]
                                    },
                                    0,
                                ]
                            },
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "log",
                            "cond": {
                                "$gt": [{"$ifNull": ["$$log.late_minutes", 0]}, 0]
                            },
                        }
                    }
                },
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$round": [{"$divide": ["$present_count", "$headcount"]}, 4]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"days": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": {"$round": ["$present_count", 2]},
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {
                    "$cond": [
                        {"$eq": ["$moving_avg_7d", None]},
                        None,
                        {"$round": ["$moving_avg_7d", 4]},
                    ]
                },
            }
        },
        {"$sort": {"date": 1}},
    ]

    try:
        items = list(d.aggregate(pipeline, allowDiskUse=True))
    except Exception:
        raise HTTPException(
            503,
            "Trend aggregation failed. Check MongoDB version and aggregation support.",
        )

    return {"department": department, "items": items}


# =========================================================
# Explain endpoint
# =========================================================

@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[str] = None,
    from_date: Optional[date] = Query(None, alias="from"),
    to_date: Optional[date] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    d = require_db()

    allowed = {
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    }

    if endpoint not in allowed:
        raise HTTPException(422, "unsupported endpoint")

    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from must not be after date_to")

        query = {}

        if emp_code is not None:
            query["emp_code"] = emp_code

        if date_from is not None or date_to is not None:
            query["date"] = {}

            if date_from is not None:
                query["date"]["$gte"] = date_from.isoformat()

            if date_to is not None:
                query["date"]["$lte"] = date_to.isoformat()

        if status is not None:
            if status not in STATUSES:
                raise HTTPException(422, "invalid status")
            query["status"] = status

        explain = d.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": query,
                "sort": {"date": -1, "emp_code": 1},
                "skip": (page - 1) * page_size,
                "limit": page_size,
            },
            verbosity="executionStats",
        )

        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain,
        }

    if endpoint == "department_trend":
        if department is None or from_date is None or to_date is None:
            raise HTTPException(422, "department, from, and to are required")

        if to_date < from_date or (to_date - from_date).days + 1 > 92:
            raise HTTPException(422, "invalid trend date range")

        query = {
            "date": {
                "$gte": from_date.isoformat(),
                "$lte": to_date.isoformat(),
            }
        }

        explain = d.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": query,
                "sort": {"date": 1},
            },
            verbosity="executionStats",
        )

        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain,
        }

    if month is None:
        raise HTTPException(422, "month is required")

    first, last = month_dates(month)

    if endpoint == "employee_monthly":
        if emp_code is None:
            raise HTTPException(422, "emp_code is required")

        pipeline = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {
                        "$gte": first.isoformat(),
                        "$lte": last.isoformat(),
                    },
                }
            },
            {"$group": {"_id": "$emp_code", "count": {"$sum": 1}}},
        ]

    elif endpoint == "department_summary":
        pipeline = [
            {
                "$match": {
                    "date": {
                        "$gte": first.isoformat(),
                        "$lte": last.isoformat(),
                    }
                }
            },
            {"$group": {"_id": "$emp_code", "count": {"$sum": 1}}},
        ]

    else:
        pipeline = [
            {
                "$match": {
                    "date": {
                        "$gte": first.isoformat(),
                        "$lte": last.isoformat(),
                    },
                    "late_minutes": {"$gt": 0},
                }
            },
            {"$group": {"_id": "$emp_code", "total": {"$sum": "$late_minutes"}}},
        ]

    explain = d.command(
        "explain",
        {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        },
        verbosity="executionStats",
    )

    return {
        "endpoint": endpoint,
        "collection": "attendance_logs",
        "explain": explain,
    }
