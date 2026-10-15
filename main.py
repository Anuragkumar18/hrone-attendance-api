"""
Employee Attendance & Analytics API - Production Implementation
"""
import calendar
import math
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client: MongoClient = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
VALID_PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
VALID_ALL_STATUSES = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}


# --------------------------------------------------------------------------- #
# Index Lifecycle Management
# --------------------------------------------------------------------------- #
def ensure_indexes():
    # Employees: unique emp_code, department index
    db.employees.create_index([("emp_code", ASCENDING)], unique=True)
    db.employees.create_index([("department", ASCENDING)])
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])

    # Attendance Logs: unique composite key (emp_code, date)
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    # Fast filtering & sorting for /attendance
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)])
    # Analytics indexing
    db.attendance_logs.create_index([("date", ASCENDING), ("status", ASCENDING)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Precision & Business Logic Helpers (R1 - R8)
# --------------------------------------------------------------------------- #
def round_half_up(val: float, decimals: int = 2) -> float:
    d = Decimal(str(val))
    target = Decimal("1." + "0" * decimals) if decimals > 0 else Decimal("1")
    return float(d.quantize(Decimal(1) / (10 ** decimals), rounding=ROUND_HALF_UP))


def to_epoch_millis(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def from_epoch_millis(ms: int) -> datetime:
    # Truncate fractional seconds (Rule R1)
    secs = ms // 1000
    return datetime.fromtimestamp(secs, tz=timezone.utc)


def parse_hh_mm(hh_mm: str) -> tuple[int, int]:
    parts = hh_mm.split(":")
    return int(parts[0]), int(parts[1])


def is_overnight_shift(shift_start: str, shift_end: str) -> bool:
    s_h, s_m = parse_hh_mm(shift_start)
    e_h, e_m = parse_hh_mm(shift_end)
    return (e_h, e_m) <= (s_h, s_m)


def determine_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """R1: The record's date is the IST calendar date of punch_in.
    Overnight exception: if punch-in IST time is earlier than shift_end, it belongs to previous day.
    """
    dt_ist = punch_in_utc.astimezone(IST)
    ist_date = dt_ist.date()
    if is_overnight_shift(shift_start, shift_end):
        e_h, e_m = parse_hh_mm(shift_end)
        shift_end_time = time(e_h, e_m)
        if dt_ist.time() < shift_end_time:
            ist_date = ist_date - timedelta(days=1)
    return ist_date.isoformat()


def compute_late_minutes(punch_in_utc: datetime, attendance_date_str: str, shift_start: str) -> int:
    """R2: Grace period 10 minutes.
    Strictly > 10 min late -> whole minutes (floor) since shift_start.
    """
    att_date = date.fromisoformat(attendance_date_str)
    s_h, s_m = parse_hh_mm(shift_start)
    shift_start_dt_ist = datetime.combine(att_date, time(s_h, s_m), tzinfo=IST)
    punch_in_ist = punch_in_utc.astimezone(IST)

    diff_sec = (punch_in_ist - shift_start_dt_ist).total_seconds()
    if diff_sec > 600:  # strictly more than 10 minutes (600s)
        return int(math.floor(diff_sec / 60))
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """R4: (punch_out - punch_in) in seconds / 3600, rounded 2 decimals half-up."""
    sec = (punch_out_utc - punch_in_utc).total_seconds()
    raw_hours = sec / 3600.0
    return round_half_up(raw_hours, 2)


def compute_overtime(punch_out_utc: datetime, attendance_date_str: str, shift_start: str, shift_end: str) -> int:
    """R3: Overtime = whole minutes (floor) between shift_end and punch_out. Counted only if >= 30, else 0."""
    att_date = date.fromisoformat(attendance_date_str)
    e_h, e_m = parse_hh_mm(shift_end)
    if is_overnight_shift(shift_start, shift_end):
        shift_end_date = att_date + timedelta(days=1)
    else:
        shift_end_date = att_date

    shift_end_dt_ist = datetime.combine(shift_end_date, time(e_h, e_m), tzinfo=IST)
    punch_out_ist = punch_out_utc.astimezone(IST)

    diff_sec = (punch_out_ist - shift_end_dt_ist).total_seconds()
    if diff_sec >= 1800:  # >= 30 minutes
        return int(math.floor(diff_sec / 60))
    return 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: half_day = true when rounded work_hours < 4.50."""
    if work_hours is None:
        return False
    return work_hours < 4.50


def serialize_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Formats attendance record for OpenAPI response."""
    history = []
    for h in doc.get("history", []):
        serialized_changes = {}
        for k, v in h.get("changes", {}).items():
            f_val = v.get("from")
            t_val = v.get("to")
            if k in ("punch_in", "punch_out"):
                f_val = to_epoch_millis(f_val) if isinstance(f_val, datetime) else f_val
                t_val = to_epoch_millis(t_val) if isinstance(t_val, datetime) else t_val
            serialized_changes[k] = {"from": f_val, "to": t_val}

        history.append({
            "at": to_epoch_millis(h["at"]) if isinstance(h["at"], datetime) else h["at"],
            "by": h["by"],
            "reason": h["reason"],
            "changes": serialized_changes
        })

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_epoch_millis(doc.get("punch_in")),
        "punch_out": to_epoch_millis(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history
    }


# --------------------------------------------------------------------------- #
# Pydantic Schemas
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    status: str = Field("PRESENT", pattern=r"^(PRESENT|WFH|ON_DUTY)$")


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    status: Optional[str] = Field(None, pattern=r"^(PRESENT|WFH|ON_DUTY|ABSENT|LEAVE)$")
    punch_in: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    if body.shift_start == body.shift_end:
        raise HTTPException(status_code=422, detail="shift_start must differ from shift_end")

    try:
        date.fromisoformat(body.joined_on)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid joined_on format")

    now = datetime.now(timezone.utc)
    # Truncate fractional seconds
    now = now.replace(microsecond=0)
    doc = body.model_dump()
    doc["created_at"] = now

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")

    doc.pop("_id", None)
    doc["created_at"] = to_epoch_millis(now)
    return doc


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    items = []
    for emp in cursor:
        emp["created_at"] = to_epoch_millis(emp["created_at"])
        items.append(emp)
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        p_in_utc = from_epoch_millis(body.punched_at)
    else:
        p_in_utc = datetime.now(timezone.utc).replace(microsecond=0)

    att_date = determine_attendance_date(p_in_utc, emp["shift_start"], emp["shift_end"])
    late_min = compute_late_minutes(p_in_utc, att_date, emp["shift_start"])

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": p_in_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Already punched in for this attendance date")

    return serialize_doc(doc)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        p_out_utc = from_epoch_millis(body.punched_at)
    else:
        p_out_utc = datetime.now(timezone.utc).replace(microsecond=0)

    # Find the most recent record whose punch_in <= punched_at
    target_record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": p_out_utc}},
        sort=[("punch_in", DESCENDING)],
    )

    if not target_record:
        raise HTTPException(status_code=404, detail="No matching punch-in found")

    if target_record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Record is already punched out")

    p_in_utc = target_record["punch_in"]
    if p_in_utc.tzinfo is None:
        p_in_utc = p_in_utc.replace(tzinfo=timezone.utc)

    diff_seconds = (p_out_utc - p_in_utc).total_seconds()
    if diff_seconds <= 0 or diff_seconds > 86400:
        raise HTTPException(status_code=422, detail="punch_out must be after punch_in and within 24 hours")

    work_hrs = compute_work_hours(p_in_utc, p_out_utc)
    ot_min = compute_overtime(p_out_utc, target_record["date"], emp["shift_start"], emp["shift_end"])
    is_half = compute_half_day(work_hrs)

    updated = db.attendance_logs.find_one_and_update(
        {"_id": target_record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": p_out_utc,
                "work_hours": work_hrs,
                "overtime_minutes": ot_min,
                "half_day": is_half,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(status_code=409, detail="Record was concurrently punched out")

    return serialize_doc(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from cannot be greater than date_to")

    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        if status not in VALID_ALL_STATUSES:
            raise HTTPException(status_code=422, detail="Invalid status")
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(skip).limit(page_size)

    items = [serialize_doc(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    new_status = body.status if body.status is not None else record["status"]
    new_p_in = from_epoch_millis(body.punch_in) if body.punch_in is not None else record.get("punch_in")
    new_p_out = from_epoch_millis(body.punch_out) if body.punch_out is not None else record.get("punch_out")

    # Ensure UTC timezone awareness
    if new_p_in and new_p_in.tzinfo is None:
        new_p_in = new_p_in.replace(tzinfo=timezone.utc)
    if new_p_out and new_p_out.tzinfo is None:
        new_p_out = new_p_out.replace(tzinfo=timezone.utc)

    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(status_code=422, detail="Punch times cannot be supplied when setting ABSENT or LEAVE")
        new_p_in = None
        new_p_out = None
    else:
        if not new_p_in:
            raise HTTPException(status_code=422, detail="Presence status requires a punch_in")

    if new_p_in:
        actual_att_date = determine_attendance_date(new_p_in, emp["shift_start"], emp["shift_end"])
        if actual_att_date != date:
            raise HTTPException(status_code=422, detail="punch_in must stay on the record's attendance date")

    if new_p_in and new_p_out:
        diff_sec = (new_p_out - new_p_in).total_seconds()
        if diff_sec <= 0 or diff_sec > 86400:
            raise HTTPException(status_code=422, detail="punch_out must be after punch_in and within 24 hours")

    # Compute final derived fields
    if new_status in ("ABSENT", "LEAVE"):
        f_late = 0
        f_work_hrs = None
        f_ot = 0
        f_half = False
    else:
        f_late = compute_late_minutes(new_p_in, date, emp["shift_start"])
        if new_p_out:
            f_work_hrs = compute_work_hours(new_p_in, new_p_out)
            f_ot = compute_overtime(new_p_out, date, emp["shift_start"], emp["shift_end"])
            f_half = compute_half_day(f_work_hrs)
        else:
            f_work_hrs = None
            f_ot = 0
            f_half = False

    # Compare changes
    changes = {}
    if new_status != record.get("status"):
        changes["status"] = {"from": record.get("status"), "to": new_status}
    if new_p_in != record.get("punch_in"):
        changes["punch_in"] = {"from": record.get("punch_in"), "to": new_p_in}
    if new_p_out != record.get("punch_out"):
        changes["punch_out"] = {"from": record.get("punch_out"), "to": new_p_out}
    if f_late != record.get("late_minutes", 0):
        changes["late_minutes"] = {"from": record.get("late_minutes", 0), "to": f_late}
    if f_work_hrs != record.get("work_hours"):
        changes["work_hours"] = {"from": record.get("work_hours"), "to": f_work_hrs}
    if f_ot != record.get("overtime_minutes", 0):
        changes["overtime_minutes"] = {"from": record.get("overtime_minutes", 0), "to": f_ot}
    if f_half != record.get("half_day", False):
        changes["half_day"] = {"from": record.get("half_day", False), "to": f_half}

    if not changes:
        raise HTTPException(status_code=422, detail="Request made no changes to the record")

    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Optimistic locking using current history length to prevent concurrent update races
    curr_history_len = len(record.get("history", []))
    res = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            f"history.{curr_history_len}": {"$exists": False},
        },
        {
            "$set": {
                "status": new_status,
                "punch_in": new_p_in,
                "punch_out": new_p_out,
                "work_hours": f_work_hrs,
                "late_minutes": f_late,
                "overtime_minutes": f_ot,
                "half_day": f_half,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if not res:
        raise HTTPException(status_code=409, detail="Concurrent regularization conflict")

    return serialize_doc(res)


# --------------------------------------------------------------------------- #
# Analytics Pipelines
# --------------------------------------------------------------------------- #
def get_month_range(month_str: str) -> tuple[str, str, int, int]:
    try:
        y, m = map(int, month_str.split("-"))
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid month format")
    last_day = calendar.monthrange(y, m)[1]
    return f"{month_str}-01", f"{month_str}-{last_day:02d}", y, m


def count_working_days(from_date: date, to_date: date) -> int:
    cur = from_date
    w_days = 0
    while cur <= to_date:
        if cur.weekday() < 5:  # 0 to 4 is Mon to Fri
            w_days += 1
        cur += timedelta(days=1)
    return w_days


def build_employee_monthly_pipeline(emp_code: str, month: str):
    start_date, end_date, _, _ = get_month_range(month)
    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_date, "$lte": end_date},
            }
        },
        {
            "$addFields": {
                "date_obj": {"$dateFromString": {"dateString": "$date"}},
            }
        },
        {
            "$addFields": {
                "day_of_week": {"$isoDayOfWeek": "$date_obj"},
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
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$lte": ["$day_of_week", 5]},
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    start_date_str, end_date_str, y, m = get_month_range(month)
    last_d = date(y, m, calendar.monthrange(y, m)[1])
    first_d = date(y, m, 1)
    joined_d = date.fromisoformat(emp["joined_on"])

    eff_start = max(first_d, joined_d)
    working_days = count_working_days(eff_start, last_d) if eff_start <= last_d else 0

    pipe = build_employee_monthly_pipeline(emp_code