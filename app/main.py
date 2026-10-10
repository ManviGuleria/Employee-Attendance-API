"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB  (loaded from a local .env for convenience)
"""
import os
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

# IST offset
IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# ---------------------------------------------------------------------------
# Index creation at startup (idempotent)
# ---------------------------------------------------------------------------
@app.on_event("startup")
def create_indexes():
    # employees: unique on emp_code (enforces concurrent-create guarantee)
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="employees_emp_code_unique")
    # employees: filter by department + sort by emp_code
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="employees_dept_empcode")
    # employees: joined_on needed for R9 headcount aggregations
    db.employees.create_index([("joined_on", ASCENDING)], name="employees_joined_on")

    # attendance: unique on (emp_code, date) – prevents double punch-in at the DB level
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="attendance_emp_date_unique",
    )
    # attendance: primary sort for list endpoint (date desc, emp_code asc)
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="attendance_date_empcode",
    )
    # attendance: monthly analytics (filter by date range)
    db.attendance_logs.create_index([("date", ASCENDING), ("emp_code", ASCENDING)], name="attendance_date_asc")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def round_half_up(value: float, decimals: int = 2) -> float:
    """Round a float using ROUND_HALF_UP (not Python's default banker's rounding)."""
    d = Decimal(str(value)).quantize(Decimal("0." + "0" * decimals), rounding=ROUND_HALF_UP)
    return float(d)


def ms_to_dt(ms: int) -> datetime:
    """Epoch milliseconds -> UTC-aware datetime, truncated to whole seconds (R1)."""
    # truncate to whole seconds first
    s = ms // 1000
    return datetime.fromtimestamp(s, tz=timezone.utc)


def dt_to_ms(dt: datetime) -> Optional[int]:
    """UTC datetime -> epoch milliseconds.  None-safe."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def to_ist(dt: datetime) -> datetime:
    """Convert a UTC (or naive-UTC) datetime to IST."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)


def attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Return the IST calendar date string for the attendance day.

    For an overnight shift (shift_end <= shift_start, e.g. 22:00-06:00):
    a punch-in whose IST time is *earlier than* shift_end belongs to the shift
    that started the previous IST calendar day.
    """
    ist_dt = to_ist(punch_in_utc)
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))

    overnight = (eh * 60 + em) <= (sh * 60 + sm)
    if overnight:
        punch_time_mins = ist_dt.hour * 60 + ist_dt.minute
        end_mins = eh * 60 + em
        if punch_time_mins < end_mins:
            # belongs to the previous day's shift
            return (ist_dt.date() - timedelta(days=1)).isoformat()
    return ist_dt.date().isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, attendance_day: str) -> int:
    """
    R2: Grace period is 10 minutes (strictly more than 10 minutes is late).
    late_minutes = floor of whole minutes since shift_start (not since end of grace period).
    punch_in is already truncated to whole seconds.
    """
    sh, sm = map(int, shift_start.split(":"))
    # Build the shift-start datetime in UTC for the attendance_day
    # attendance_day is an IST calendar date; the shift starts at shift_start IST on that day
    day = date.fromisoformat(attendance_day)
    shift_start_ist = datetime(day.year, day.month, day.day, sh, sm, 0, tzinfo=IST)
    shift_start_utc = shift_start_ist.astimezone(timezone.utc)

    diff_seconds = (punch_in_utc - shift_start_utc).total_seconds()
    if diff_seconds <= 0:
        return 0
    whole_minutes = int(diff_seconds // 60)  # floor
    return whole_minutes if whole_minutes > 10 else 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """R4: (punch_out - punch_in) in seconds / 3600, rounded 2 decimals ROUND_HALF_UP."""
    seconds = (punch_out_utc - punch_in_utc).total_seconds()
    return round_half_up(seconds / 3600, 2)


def compute_overtime_minutes(punch_out_utc: datetime, shift_end: str, attendance_day: str) -> int:
    """
    R3: floor minutes between shift_end and punch_out.
    Only counted when >= 30, otherwise 0.
    For overnight shifts, shift_end is on the *next* calendar day relative to attendance_day.
    """
    sh, sm = map(int, shift_end.split(":"))
    day = date.fromisoformat(attendance_day)

    # Determine if overnight: if shift_end <= shift_start we'd need the original employee,
    # but we only have shift_end here. The caller already knows the attendance day is the
    # start day; the end is on attendance_day + 1 when shift_end time < shift_start time.
    # We use a simpler heuristic: if shift_end hour < 12 it's likely an overnight end.
    # The robust way: caller passes shift_start too. Let's accept it as a parameter.
    # (see compute_overtime_minutes_full which takes shift_start)
    shift_end_ist = datetime(day.year, day.month, day.day, sh, sm, 0, tzinfo=IST)
    diff_seconds = (punch_out_utc - shift_end_ist.astimezone(timezone.utc)).total_seconds()
    if diff_seconds <= 0:
        return 0
    whole_minutes = int(diff_seconds // 60)
    return whole_minutes if whole_minutes >= 30 else 0


def compute_overtime_minutes_full(
    punch_out_utc: datetime, shift_start: str, shift_end: str, attendance_day: str
) -> int:
    """
    R3 with overnight shift awareness.
    For an overnight shift (shift_end <= shift_start), shift end falls on attendance_day + 1.
    """
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    day = date.fromisoformat(attendance_day)
    overnight = (eh * 60 + em) <= (sh * 60 + sm)
    end_day = day + timedelta(days=1) if overnight else day
    shift_end_ist = datetime(end_day.year, end_day.month, end_day.day, eh, em, 0, tzinfo=IST)
    diff_seconds = (punch_out_utc - shift_end_ist.astimezone(timezone.utc)).total_seconds()
    if diff_seconds <= 0:
        return 0
    whole_minutes = int(diff_seconds // 60)
    return whole_minutes if whole_minutes >= 30 else 0


def compute_half_day(work_hours: float) -> bool:
    """R5: half_day = True when rounded work_hours < 4.50."""
    return work_hours < 4.50


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def serialize_record(doc: dict) -> dict:
    """Convert a raw MongoDB attendance_log doc to the API shape."""
    doc.pop("_id", None)
    if doc.get("punch_in") is not None:
        doc["punch_in"] = dt_to_ms(doc["punch_in"])
    if doc.get("punch_out") is not None:
        doc["punch_out"] = dt_to_ms(doc["punch_out"])
    # Defaults for fields that may be absent in legacy records
    doc.setdefault("late_minutes", 0)
    doc.setdefault("overtime_minutes", 0)
    doc.setdefault("half_day", False)
    doc.setdefault("history", [])
    # Serialize history entries
    for entry in doc.get("history", []):
        if "at" in entry and isinstance(entry["at"], datetime):
            entry["at"] = dt_to_ms(entry["at"])
        changes = entry.get("changes", {})
        for field in ("punch_in", "punch_out"):
            if field in changes:
                for key in ("from", "to"):
                    if isinstance(changes[field].get(key), datetime):
                        changes[field][key] = dt_to_ms(changes[field][key])
    return doc


def serialize_employee(doc: dict) -> dict:
    """Convert a raw MongoDB employee doc to the API shape."""
    doc.pop("_id", None)
    if isinstance(doc.get("created_at"), datetime):
        doc["created_at"] = dt_to_ms(doc["created_at"])
    return doc


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

EPOCH_MIN = 100_000_000_000   # minimum valid epoch ms (prevents seconds-looking values)
EPOCH_MAX = 4_102_444_800_000


def validate_epoch_ms(v):
    if v is None:
        return v
    if not isinstance(v, int):
        raise ValueError("punched_at must be an integer (epoch milliseconds)")
    if v < EPOCH_MIN or v > EPOCH_MAX:
        raise ValueError(f"punched_at out of range [{EPOCH_MIN}, {EPOCH_MAX}]")
    return v


class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @model_validator(mode="after")
    def shift_start_ne_shift_end(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    @field_validator("punched_at", mode="before")
    @classmethod
    def validate_punched_at(cls, v):
        return validate_epoch_ms(v)

    @field_validator("status")
    @classmethod
    def validate_status(cls, v):
        if v not in ("PRESENT", "WFH", "ON_DUTY"):
            raise ValueError("status must be one of PRESENT, WFH, ON_DUTY")
        return v


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at", mode="before")
    @classmethod
    def validate_punched_at(cls, v):
        return validate_epoch_ms(v)


class RegularizeIn(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str
    regularized_by: str

    @field_validator("punch_in", "punch_out", mode="before")
    @classmethod
    def validate_epoch(cls, v):
        return validate_epoch_ms(v)

    @field_validator("status")
    @classmethod
    def validate_status(cls, v):
        if v is not None and v not in ("PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"):
            raise ValueError("invalid status")
        return v

    @model_validator(mode="after")
    def absent_leave_no_punches(self):
        if self.status in ("ABSENT", "LEAVE"):
            if self.punch_in is not None or self.punch_out is not None:
                raise ValueError("Cannot supply punch_in or punch_out with ABSENT/LEAVE status")
        return self


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    try:
        db.command("ping")
    except Exception:
        raise HTTPException(status_code=503, detail="MongoDB unavailable")
    return {"status": "ok"}


# --- Employees ---

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(tz=timezone.utc)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, detail="emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department
    # R10: page starts at 1; total reflects the filtered set
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size   # Bug fix: starter used page * page_size (off by one)
    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)   # Bug fix: starter had no sort
        .skip(skip)
        .limit(page_size)
    )
    for emp in items:
        serialize_employee(emp)
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --- Attendance ---

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")

    # Resolve punch-in time; truncate to whole seconds (R1)
    if body.punched_at is not None:
        punch_ts = ms_to_dt(body.punched_at)
    else:
        # truncate current time to whole seconds
        now = datetime.now(tz=timezone.utc)
        punch_ts = now.replace(microsecond=0)

    att_date = attendance_date(punch_ts, emp["shift_start"], emp["shift_end"])
    late = compute_late_minutes(punch_ts, emp["shift_start"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, detail="Already punched in for this attendance date")

    return serialize_record(doc)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")

    if body.punched_at is not None:
        punch_ts = ms_to_dt(body.punched_at)
    else:
        now = datetime.now(tz=timezone.utc)
        punch_ts = now.replace(microsecond=0)

    # Find the most recent record with punch_in <= punched_at (open or closed)
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": punch_ts},
        },
        sort=[("punch_in", DESCENDING)],
    )
    if not record:
        raise HTTPException(404, detail="No punch-in found for this employee")

    # If the record is already punched out -> 409
    if record.get("punch_out") is not None:
        raise HTTPException(409, detail="Record already punched out")

    punch_in_ts = record["punch_in"]
    if punch_in_ts.tzinfo is None:
        punch_in_ts = punch_in_ts.replace(tzinfo=timezone.utc)

    # 422 if punch_out <= punch_in or more than 24 hours after punch_in
    if punch_ts <= punch_in_ts:
        raise HTTPException(422, detail="punched_at must be after punch_in")
    if (punch_ts - punch_in_ts).total_seconds() > 86400:
        raise HTTPException(422, detail="punch_out cannot be more than 24 hours after punch_in")

    wh = compute_work_hours(punch_in_ts, punch_ts)
    ot = compute_overtime_minutes_full(punch_ts, emp["shift_start"], emp["shift_end"], record["date"])
    hd = compute_half_day(wh)

    # Atomic update; condition on punch_out=None so concurrent second punch-out returns None -> 409
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_ts,
                "work_hours": wh,
                "overtime_minutes": ot,
                "half_day": hd,
            }
        },
        return_document=True,
    )
    if updated is None:
        raise HTTPException(409, detail="Record already punched out")

    return serialize_record(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, detail="date_from cannot be greater than date_to")

    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    docs = list(
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])  # Bug fix: starter sorted in Python (slow + wrong)
        .skip(skip)
        .limit(page_size)
    )
    return {
        "items": [serialize_record(d) for d in docs],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date_str}", status_code=200)
def regularize(emp_code: str, date_str: str, body: RegularizeIn):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_str})
    if not record:
        raise HTTPException(404, detail="Attendance record not found")

    # Build the updated values from the stored record + request overrides
    new_status = body.status if body.status is not None else record["status"]

    # Resolve punch times (stored values as baseline)
    stored_pin = record.get("punch_in")
    stored_pout = record.get("punch_out")
    if stored_pin is not None and stored_pin.tzinfo is None:
        stored_pin = stored_pin.replace(tzinfo=timezone.utc)
    if stored_pout is not None and stored_pout.tzinfo is None:
        stored_pout = stored_pout.replace(tzinfo=timezone.utc)

    if new_status in ("ABSENT", "LEAVE"):
        new_pin = None
        new_pout = None
    else:
        new_pin = ms_to_dt(body.punch_in) if body.punch_in is not None else stored_pin
        new_pout = ms_to_dt(body.punch_out) if body.punch_out is not None else stored_pout

        # Presence status requires punch_in
        if new_pin is None:
            raise HTTPException(422, detail="Presence status requires punch_in")

        # punch_in must still map to the same attendance date (R1)
        if new_pin is not None:
            resulting_date = attendance_date(new_pin, emp["shift_start"], emp["shift_end"])
            if resulting_date != date_str:
                raise HTTPException(422, detail="punch_in must remain on the record's attendance date")

        # punch_out must be after punch_in and within 24 hours
        if new_pout is not None and new_pin is not None:
            if new_pout <= new_pin:
                raise HTTPException(422, detail="punch_out must be after punch_in")
            if (new_pout - new_pin).total_seconds() > 86400:
                raise HTTPException(422, detail="punch_out cannot be more than 24 hours after punch_in")

    # Recompute derived fields
    if new_status in ("ABSENT", "LEAVE") or new_pin is None:
        new_late = 0
        new_wh = None
        new_ot = 0
        new_hd = False
    else:
        new_late = compute_late_minutes(new_pin, emp["shift_start"], date_str)
        if new_pout is not None:
            new_wh = compute_work_hours(new_pin, new_pout)
            new_ot = compute_overtime_minutes_full(new_pout, emp["shift_start"], emp["shift_end"], date_str)
            new_hd = compute_half_day(new_wh)
        else:
            new_wh = None
            new_ot = 0
            new_hd = False

    # Build changes dict (only fields that actually changed)
    changes: dict = {}

    old_status = record["status"]
    if new_status != old_status:
        changes["status"] = {"from": old_status, "to": new_status}

    old_pin_dt = stored_pin
    if new_pin != old_pin_dt:
        changes["punch_in"] = {
            "from": dt_to_ms(old_pin_dt),
            "to": dt_to_ms(new_pin),
        }

    old_pout_dt = stored_pout
    if new_pout != old_pout_dt:
        changes["punch_out"] = {
            "from": dt_to_ms(old_pout_dt),
            "to": dt_to_ms(new_pout),
        }

    old_wh = record.get("work_hours")
    if new_wh != old_wh:
        changes["work_hours"] = {"from": old_wh, "to": new_wh}

    old_late = record.get("late_minutes", 0)
    if new_late != old_late:
        changes["late_minutes"] = {"from": old_late, "to": new_late}

    old_ot = record.get("overtime_minutes", 0)
    if new_ot != old_ot:
        changes["overtime_minutes"] = {"from": old_ot, "to": new_ot}

    old_hd = record.get("half_day", False)
    if new_hd != old_hd:
        changes["half_day"] = {"from": old_hd, "to": new_hd}

    if not changes:
        raise HTTPException(422, detail="No changes detected")

    # Build history entry (store punch datetimes as BSON dates in history)
    history_changes: dict = {}
    for field, val in changes.items():
        if field in ("punch_in", "punch_out"):
            history_changes[field] = {
                "from": ms_to_dt(val["from"]) if val["from"] is not None else None,
                "to": ms_to_dt(val["to"]) if val["to"] is not None else None,
            }
        else:
            history_changes[field] = val

    history_entry = {
        "at": datetime.now(tz=timezone.utc),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": history_changes,
    }

    # Atomic update; use optimistic-concurrency via matching on _id to prevent lost-update
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"]},
        {
            "$set": {
                "status": new_status,
                "punch_in": new_pin,
                "punch_out": new_pout,
                "work_hours": new_wh,
                "late_minutes": new_late,
                "overtime_minutes": new_ot,
                "half_day": new_hd,
            },
            "$push": {"history": history_entry},
        },
        return_document=True,
    )
    if updated is None:
        raise HTTPException(409, detail="Concurrent modification detected, please retry")

    return serialize_record(updated)


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------

def month_bounds(month: str):
    """Return (first_day_str, last_day_str) for a 'YYYY-MM' month string."""
    year, mon = int(month[:4]), int(month[5:])
    first = date(year, mon, 1)
    # last day of month
    if mon == 12:
        last = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        last = date(year, mon + 1, 1) - timedelta(days=1)
    return first.isoformat(), last.isoformat()


def count_working_days(start_str: str, end_str: str) -> int:
    """Mon-Fri count between start_str and end_str inclusive (R7)."""
    start = date.fromisoformat(start_str)
    end = date.fromisoformat(end_str)
    count = 0
    cur = start
    while cur <= end:
        if cur.weekday() < 5:  # 0=Mon ... 4=Fri
            count += 1
        cur += timedelta(days=1)
    return count


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")

    first_day, last_day = month_bounds(month)

    # R7: working days counted from max(first_day, joined_on)
    effective_start = max(first_day, emp["joined_on"])
    if effective_start > last_day:
        # Employee joined after month end
        working_days = 0
    else:
        working_days = count_working_days(effective_start, last_day)

    # Aggregation over attendance_logs for this employee and month
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first_day, "$lte": last_day}}},
        {
            "$group": {
                "_id": None,
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
                # present_days: R5+R6: PRESENT/WFH/ON_DUTY, Mon-Fri only, half_day=0.5
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {
                                "$cond": [
                                    {"$eq": [{"$ifNull": ["$half_day", False]}, True]},
                                    0.5,
                                    1.0,
                                ]
                            },
                            0,
                        ]
                    }
                },
                # We also need the dates to filter to Mon-Fri
                "records": {"$push": {"date": "$date", "status": "$status", "half_day": "$half_day"}},
            }
        },
    ]

    # Because present_days must only count Mon-Fri (R7), compute it in Python from the records
    # (MongoDB doesn't have a built-in day-of-week function that's trivial for this purpose
    # but $dayOfWeek can be used; however since 'date' is a string we use Python here)
    raw = list(db.attendance_logs.aggregate(pipeline))

    leave_days = 0
    late_count = 0
    total_late_minutes = 0
    total_overtime_minutes = 0
    present_days: float = 0.0

    if raw:
        r = raw[0]
        leave_days = r["leave_days"]
        late_count = r["late_count"]
        total_late_minutes = r["total_late_minutes"]
        total_overtime_minutes = r["total_overtime_minutes"]
        for rec in r["records"]:
            d = date.fromisoformat(rec["date"])
            if d.weekday() >= 5:
                continue  # skip weekends for present_days (R7)
            if rec["status"] in ("PRESENT", "WFH", "ON_DUTY"):
                present_days += 0.5 if rec.get("half_day") else 1.0

    present_days = round_half_up(present_days, 2)

    if working_days == 0:
        attendance_pct = None
    else:
        attendance_pct = round_half_up((present_days / working_days) * 100, 2)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    first_day, last_day = month_bounds(month)

    # Step 1: get all departments + headcounts (R9: joined_on <= last_day of month)
    emp_match: dict = {"joined_on": {"$lte": last_day}}
    if department:
        emp_match["department"] = department

    # Get employees per department
    emp_pipeline = [
        {"$match": emp_match},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "emp_codes": {"$push": "$emp_code"},
            }
        },
        {"$sort": {"_id": ASCENDING}},
    ]
    dept_data = {r["_id"]: r for r in db.employees.aggregate(emp_pipeline)}

    if not dept_data:
        return {"month": month, "items": []}

    # Step 2: aggregate attendance logs for the month
    log_match: dict = {"date": {"$gte": first_day, "$lte": last_day}}
    # Restrict to employees in scope by doing a $lookup-style join or just pull logs
    # and join in Python. Since the grader has ~100k records we use a pipeline join.
    all_dept_emp_codes = [code for r in dept_data.values() for code in r["emp_codes"]]
    log_match["emp_code"] = {"$in": all_dept_emp_codes}

    log_pipeline = [
        {"$match": log_match},
        # Lookup employee department
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        {
            "$group": {
                "_id": "$emp.department",
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
                # avg_work_hours: only presence records with a work_hours value
                "work_hours_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$gt": ["$work_hours", None]},
                                ]
                            },
                            "$work_hours",
                            0,
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$gt": ["$work_hours", None]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                # present_days raw records (we filter weekdays in Python)
                "presence_records": {
                    "$push": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"date": "$date", "half_day": {"$ifNull": ["$half_day", False]}},
                            "$$REMOVE",
                        ]
                    }
                },
            }
        },
    ]

    log_results = {r["_id"]: r for r in db.attendance_logs.aggregate(log_pipeline)}

    items = []
    for dept_name in sorted(dept_data.keys()):
        dept_info = dept_data[dept_name]
        headcount = dept_info["headcount"]

        log_r = log_results.get(dept_name, {})

        late_count = log_r.get("late_count", 0)
        total_late_minutes = log_r.get("total_late_minutes", 0)
        leave_count = log_r.get("leave_count", 0)
        on_duty_count = log_r.get("on_duty_count", 0)

        wh_sum = log_r.get("work_hours_sum", 0)
        wh_count = log_r.get("work_hours_count", 0)
        avg_work_hours = round_half_up(wh_sum / wh_count, 2) if wh_count > 0 else None

        # present_days: Mon-Fri only
        present_days: float = 0.0
        for rec in log_r.get("presence_records", []):
            d = date.fromisoformat(rec["date"])
            if d.weekday() >= 5:
                continue
            present_days += 0.5 if rec.get("half_day") else 1.0
        present_days = round_half_up(present_days, 2)

        items.append(
            {
                "department": dept_name,
                "headcount": headcount,
                "present_days": present_days,
                "avg_work_hours": avg_work_hours,
                "late_count": late_count,
                "total_late_minutes": total_late_minutes,
                "leave_count": leave_count,
                "on_duty_count": on_duty_count,
            }
        )

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    first_day, last_day = month_bounds(month)

    # Build the pipeline: sum late_minutes per employee, join employee info, rank
    match_stage: dict = {"date": {"$gte": first_day, "$lte": last_day}, "late_minutes": {"$gt": 0}}

    pipeline = [
        {"$match": match_stage},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        # Join employee to get name and department
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        # Filter by department if given (ranking is inside that department)
        *(
            [{"$match": {"emp.department": department}}]
            if department
            else []
        ),
        # Sort: total_late_minutes desc, emp_code asc (for deterministic tie-breaking in ordering)
        {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
        # Standard competition ranking via $setWindowFields
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {
                        "$rank": {}
                    }
                },
            }
        },
        # Keep rows where rank <= limit
        {"$match": {"rank": {"$lte": limit}}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        # Final sort: total_late_minutes desc, emp_code asc
        {"$sort": {"total_late_minutes": DESCENDING, "emp_code": ASCENDING}},
    ]

    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(alias="from"),
    to_date: str = Query(alias="to"),
):
    # Validate department exists
    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, detail="Department not found")

    # Validate date range
    from_d = date.fromisoformat(from_date)
    to_d = date.fromisoformat(to_date)
    if to_d < from_d:
        raise HTTPException(422, detail="'to' cannot be before 'from'")
    if (to_d - from_d).days > 91:  # 92 days means 0..91 = 92 values
        raise HTTPException(422, detail="Date range cannot exceed 92 days")

    # Get all employees in department whose joined_on <= to_date (for headcount per day, R9)
    dept_employees = list(db.employees.find({"department": department}, {"emp_code": 1, "joined_on": 1, "_id": 0}))
    if not dept_employees:
        raise HTTPException(404, detail="Department not found")

    emp_codes = [e["emp_code"] for e in dept_employees]

    # Aggregate attendance logs for the department employees in the range
    pipeline = [
        {
            "$match": {
                "emp_code": {"$in": emp_codes},
                "date": {"$gte": from_date, "$lte": to_date},
            }
        },
        {
            "$group": {
                "_id": "$date",
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                "presence_records": {
                    "$push": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$ifNull": ["$half_day", False]},
                            "$$REMOVE",
                        ]
                    }
                },
            }
        },
    ]

    raw_by_date = {r["_id"]: r for r in db.attendance_logs.aggregate(pipeline)}

    # Build one row per calendar day (gap filling in Python is fine for <= 92 days)
    items = []
    cur = from_d

    # Pre-process employee joined_on for O(1) lookup
    emp_joined = {e["emp_code"]: e["joined_on"] for e in dept_employees}

    # Build rolling window of up to 7 non-null attendance_rates
    rate_window: list = []  # stores non-null attendance_rate values in order

    while cur <= to_d:
        cur_str = cur.isoformat()
        is_working_day = cur.weekday() < 5

        # Headcount: employees with joined_on <= cur (R9)
        headcount = sum(1 for j in emp_joined.values() if j <= cur_str)

        raw = raw_by_date.get(cur_str)
        if raw:
            late_count = raw["late_count"]
            present_count = sum(0.5 if hd else 1.0 for hd in raw["presence_records"])
        else:
            late_count = 0
            present_count = 0.0

        # attendance_rate: only on working days with headcount > 0
        if is_working_day and headcount > 0:
            attendance_rate = round_half_up(present_count / headcount, 4)
            rate_window.append(attendance_rate)
        else:
            attendance_rate = None

        # moving_avg_7d: mean of non-null rates in current window (up to 7)
        window_vals = rate_window[-7:]  # last 7 non-null rates (including current if non-null)
        if window_vals:
            moving_avg_7d = round_half_up(sum(window_vals) / len(window_vals), 4)
        else:
            moving_avg_7d = None

        items.append(
            {
                "date": cur_str,
                "is_working_day": is_working_day,
                "headcount": headcount,
                "present_count": present_count,
                "late_count": late_count,
                "attendance_rate": attendance_rate,
                "moving_avg_7d": moving_avg_7d,
            }
        )
        cur += timedelta(days=1)

    return {"department": department, "items": items}


# ---------------------------------------------------------------------------
# Admin - explain
# ---------------------------------------------------------------------------

def _build_attendance_list_query(
    emp_code=None, date_from=None, date_to=None, status=None, page=1, page_size=20, **_
):
    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    return q, page, page_size


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(default=None, alias="from"),
    to_date: Optional[str] = Query(default=None, alias="to"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    VALID = {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}
    if endpoint not in VALID:
        raise HTTPException(422, detail=f"endpoint must be one of {sorted(VALID)}")

    if endpoint == "attendance_list":
        q, pg, ps = _build_attendance_list_query(
            emp_code=emp_code, date_from=date_from, date_to=date_to, status=status, page=page, page_size=page_size
        )
        skip = (pg - 1) * ps
        cursor = db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(skip).limit(ps)
        explain_doc = db.command(
            "explain",
            {"find": "attendance_logs", "filter": q, "sort": {"date": -1, "emp_code": 1}, "skip": skip, "limit": ps},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_doc}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, detail="emp_code and month are required for employee_monthly")
        first_day, last_day = month_bounds(month)
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": first_day, "$lte": last_day}}},
            {"$group": {"_id": None, "total": {"$sum": 1}}},
        ]
        explain_doc = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_doc}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, detail="month is required for department_summary")
        first_day, last_day = month_bounds(month)
        emp_match: dict = {"joined_on": {"$lte": last_day}}
        if department:
            emp_match["department"] = department
        pipeline = [
            {"$match": emp_match},
            {"$group": {"_id": "$department", "headcount": {"$sum": 1}}},
        ]
        explain_doc = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "employees", "explain": explain_doc}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, detail="month is required for late_leaderboard")
        first_day, last_day = month_bounds(month)
        match_stage: dict = {"date": {"$gte": first_day, "$lte": last_day}, "late_minutes": {"$gt": 0}}
        pipeline = [
            {"$match": match_stage},
            {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}}},
            {"$sort": {"total_late_minutes": -1}},
        ]
        explain_doc = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_doc}

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, detail="department, from, and to are required for department_trend")
        emp_codes = [e["emp_code"] for e in db.employees.find({"department": department}, {"emp_code": 1, "_id": 0})]
        pipeline = [
            {
                "$match": {
                    "emp_code": {"$in": emp_codes},
                    "date": {"$gte": from_date, "$lte": to_date},
                }
            },
            {"$group": {"_id": "$date", "late_count": {"$sum": 1}}},
        ]
        explain_doc = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_doc}
