"""Employee Attendance & Analytics API."""
import os, re, calendar
from datetime import datetime, date, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal, Any
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError
from bson import ObjectId
from bson.json_util import dumps
load_dotenv()
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), serverSelectionTimeoutMS=1500)
db = client[os.getenv("MONGO_DB", "attendance_db")]
employees = db.employees
logs = db.attendance_logs
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
STATUSES = {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

@app.on_event("startup")
def ensure_indexes():
    employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="department_joined")
    logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date")
    logs.create_index([("date", ASCENDING), ("emp_code", ASCENDING)], name="attendance_date_employee")
    logs.create_index([("status", ASCENDING), ("date", ASCENDING)], name="attendance_status_date")
    logs.create_index([("emp_code", ASCENDING), ("date", DESCENDING)], name="attendance_employee_date_desc")

def now_utc():
    return datetime.now(UTC).replace(microsecond=0)

def parse_date(s: str) -> date:
    try:
        d = date.fromisoformat(s)
        if d.isoformat() != s: raise ValueError()
        return d
    except Exception:
        raise HTTPException(422, "date must be YYYY-MM-DD")

def parse_month(s: str) -> tuple[date,date]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", s or ""):
        raise HTTPException(422, "month must be YYYY-MM")
    y,m=map(int,s.split("-"))
    return date(y,m,1), date(y+1,1,1) if m==12 else date(y,m+1,1)

def epoch_ms(dt):
    if dt is None: return None
    if dt.tzinfo is None: dt=dt.replace(tzinfo=UTC)
    return int(dt.timestamp()*1000)

def from_ms(v):
    if v is None: return None
    if isinstance(v,bool) or not isinstance(v,int) or not 100_000_000_000 <= v <= 4_102_444_800_000:
        raise HTTPException(422, "timestamp must be epoch milliseconds as an integer")
    return datetime.fromtimestamp(v/1000, UTC).replace(microsecond=0)

def to_ist(dt):
    if dt is None: return None
    if dt.tzinfo is None: dt=dt.replace(tzinfo=UTC)
    return dt.astimezone(IST)

def record_date(punch_in, emp):
    local=to_ist(punch_in)
    sh,sm=map(int,emp["shift_start"].split(":"))
    eh,em=map(int,emp["shift_end"].split(":"))
    # For overnight shifts, early-morning punch-ins belong to yesterday's shift.
    d=local.date()
    if (eh,em) <= (sh,sm) and (local.hour,local.minute) < (eh,em):
        d -= timedelta(days=1)
    return d.isoformat()

def shift_start_dt(att_date, emp):
    d=parse_date(att_date);
    h,m=map(int,emp["shift_start"].split(":"))
    return datetime.combine(d,time(h,m),IST).astimezone(UTC).replace(microsecond=0)

def shift_end_dt(att_date, emp):
    d=parse_date(att_date); sh=emp["shift_start"]; en=emp["shift_end"]
    h,m=map(int,en.split(":")); eh=time(h,m)
    shh,shm=map(int,sh.split(":"))
    if (h,m) <= (shh,shm): d += timedelta(days=1)
    return datetime.combine(d,eh,IST).astimezone(UTC).replace(microsecond=0)

def late_minutes(punch_in, att_date, emp):
    delta=(punch_in-shift_start_dt(att_date,emp)).total_seconds()
    return int(delta//60) if delta > 600 else 0

def overtime_minutes(punch_out, att_date, emp):
    delta=(punch_out-shift_end_dt(att_date,emp)).total_seconds()
    mins=int(delta//60)
    return mins if mins >= 30 else 0

def round_half_up(value, places=2):
    if value is None: return None
    q=Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))

def calc_work_hours(pin,pout):
    return round_half_up((pout-pin).total_seconds()/3600,2)

def output_record(d):
    return {"emp_code":d["emp_code"],"date":d["date"],"status":d["status"],
      "punch_in":epoch_ms(d.get("punch_in")),"punch_out":epoch_ms(d.get("punch_out")),
      "work_hours":d.get("work_hours"),"late_minutes":d.get("late_minutes",0),
      "overtime_minutes":d.get("overtime_minutes",0),"half_day":d.get("half_day",False),
      "history":[{**h,"at":epoch_ms(h.get("at")),"changes":{
        k:{"from":epoch_ms(v.get("from")) if k in ("punch_in","punch_out") else v.get("from"),
           "to":epoch_ms(v.get("to")) if k in ("punch_in","punch_out") else v.get("to")}
        for k,v in h.get("changes",{}).items()}} for h in d.get("history",[])]}

def output_employee(d):
    return {k:(epoch_ms(v) if k=="created_at" else v) for k,v in d.items() if k!="_id"}

def validate_shift(v):
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d",v or ""): raise ValueError("must be HH:MM")
    return v
def validate_date_value(v):
    try:
        if date.fromisoformat(v).isoformat()!=v: raise ValueError()
    except Exception: raise ValueError("must be YYYY-MM-DD")
    return v

class EmployeeIn(BaseModel):
    model_config=ConfigDict(extra="forbid")
    emp_code:str=Field(pattern=r"^EMP\d{4,6}$")
    name:str=Field(min_length=1,max_length=100)
    email:str=Field(max_length=120,pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department:str=Field(min_length=1,max_length=50)
    shift_start:str="09:30"
    shift_end:str="18:30"
    joined_on:str
    @field_validator("shift_start","shift_end")
    @classmethod
    def shift_valid(cls,v): return validate_shift(v)
    @field_validator("joined_on")
    @classmethod
    def date_valid(cls,v): return validate_date_value(v)

class PunchInIn(BaseModel):
    model_config=ConfigDict(extra="forbid")
    emp_code:str
    punched_at:Optional[int]=Field(default=None, strict=True)
    status:Literal["PRESENT","WFH","ON_DUTY"]="PRESENT"
    @field_validator("punched_at")
    @classmethod
    def ms_valid(cls,v):
        if v is not None and (isinstance(v,bool) or not 100_000_000_000 <= v <= 4_102_444_800_000): raise ValueError("must be epoch milliseconds")
        return v

class PunchOutIn(BaseModel):
    model_config=ConfigDict(extra="forbid")
    emp_code:str
    punched_at:Optional[int]=Field(default=None, strict=True)
    @field_validator("punched_at")
    @classmethod
    def ms_valid(cls,v):
        if v is not None and (isinstance(v,bool) or not 100_000_000_000 <= v <= 4_102_444_800_000): raise ValueError("must be epoch milliseconds")
        return v

class RegularizeIn(BaseModel):
    model_config=ConfigDict(extra="forbid")
    status:Optional[Literal["PRESENT","ABSENT","LEAVE","WFH","ON_DUTY"]]=None
    punch_in:Optional[int]=Field(default=None)
    punch_out:Optional[int]=Field(default=None)
    reason:str=Field(min_length=5,max_length=200)
    regularized_by:str=Field(min_length=1,max_length=50)
    @field_validator("punch_in","punch_out")
    @classmethod
    def ms_valid(cls,v):
        if v is not None and (isinstance(v,bool) or not 100_000_000_000 <= v <= 4_102_444_800_000): raise ValueError("must be epoch milliseconds")
        return v

@app.exception_handler(RequestValidationError)
async def validation_handler(request, exc):
    return JSONResponse(status_code=422, content={"detail": str(exc)})

@app.get("/health")
def health():
    try: client.admin.command("ping")
    except PyMongoError: raise HTTPException(503,"MongoDB unavailable")
    return {"status":"ok"}

@app.post("/employees",status_code=201)
def create_employee(body:EmployeeIn):
    doc=body.model_dump(); doc["created_at"]=now_utc()
    try: employees.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409,"emp_code already exists")
    return output_employee(doc)

@app.get("/employees")
def list_employees(department:Optional[str]=None,page:int=Query(1,ge=1),page_size:int=Query(20,ge=1,le=100)):
    q={"department":department} if department else {}
    total=employees.count_documents(q)
    items=[output_employee(d) for d in employees.find(q,{"_id":0}).sort("emp_code",ASCENDING).skip((page-1)*page_size).limit(page_size)]
    return {"items":items,"total":total,"page":page,"page_size":page_size}

@app.post("/attendance/punch-in",status_code=201)
def punch_in(body:PunchInIn):
    emp=employees.find_one({"emp_code":body.emp_code})
    if not emp: raise HTTPException(404,"unknown employee")
    ts=from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    ts=ts.replace(microsecond=0)
    d=record_date(ts,emp)
    if d < emp["joined_on"]: raise HTTPException(422,"punch-in predates employment")
    doc={"emp_code":body.emp_code,"date":d,"status":body.status,"punch_in":ts,"punch_out":None,
      "work_hours":None,"late_minutes":late_minutes(ts,d,emp),"overtime_minutes":0,"half_day":False,"history":[]}
    try: logs.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409,"already punched in for this date")
    return output_record(doc)

@app.get("/attendance")
def list_attendance(emp_code:Optional[str]=None,date_from:Optional[str]=None,date_to:Optional[str]=None,
                    status:Optional[str]=None,page:int=Query(1,ge=1),page_size:int=Query(20,ge=1,le=100)):
    q={}
    if emp_code: q["emp_code"]=emp_code
    if date_from or date_to:
        q["date"]={}
        if date_from: q["date"]["$gte"]=validate_date_value(date_from)
        if date_to: q["date"]["$lte"]=validate_date_value(date_to)
        if date_from and date_to and date_from>date_to: raise HTTPException(422,"date_to must be >= date_from")
    if status:
        if status not in STATUSES: raise HTTPException(422,"invalid status")
        q["status"]=status
    total=logs.count_documents(q)
    docs=logs.find(q).sort([("date",DESCENDING),("emp_code",ASCENDING)]).skip((page-1)*page_size).limit(page_size)
    return {"items":[output_record(d) for d in docs],"total":total,"page":page,"page_size":page_size}

@app.post("/attendance/punch-out")
def punch_out(body:PunchOutIn):
    emp=employees.find_one({"emp_code":body.emp_code})
    if not emp: raise HTTPException(404,"unknown employee")
    ts=from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    ts=ts.replace(microsecond=0)
    # Find the most recent open record, which supports overnight shifts.
    rec=logs.find_one({"emp_code":body.emp_code,"punch_out":None,"status":{"$in":list(PRESENCE)}},sort=[("date",DESCENDING)])
    if not rec: raise HTTPException(404,"no open attendance record")
    pin=rec.get("punch_in")
    if not pin:
        raise HTTPException(422,"punch-in time is missing")
    if pin.tzinfo is None:
        pin = pin.replace(tzinfo=timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    if ts <= pin or (ts - pin).total_seconds() > 86400:
        raise HTTPException(422,"punch-out must be after punch-in and within 24 hours")
    hours=calc_work_hours(pin,ts)
    ot=overtime_minutes(ts,rec["date"],emp)
    update={"punch_out":ts,"work_hours":hours,"overtime_minutes":ot,"half_day":hours<4.50}
    result=logs.update_one({"_id":rec["_id"],"punch_out":None},{"$set":update})
    if result.modified_count!=1: raise HTTPException(409,"attendance was already punched out")
    rec.update(update)
    return output_record(rec)

@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code:str,date:str,body:RegularizeIn):
    att_date=date
    validate_date_value(att_date)
    emp=employees.find_one({"emp_code":emp_code})
    if not emp: raise HTTPException(404,"unknown employee")
    rec=logs.find_one({"emp_code":emp_code,"date":att_date})
    if not rec: raise HTTPException(404,"attendance record not found")
    old={"status":rec.get("status"),"punch_in":rec.get("punch_in"),"punch_out":rec.get("punch_out"),
         "work_hours":rec.get("work_hours"),"late_minutes":rec.get("late_minutes",0),
         "overtime_minutes":rec.get("overtime_minutes",0),"half_day":rec.get("half_day",False)}
    new=dict(old)
    sent=body.model_dump(exclude_unset=True)
    for k in ("status","punch_in","punch_out"):
        if k in sent:
            new[k]=from_ms(sent[k]) if k in ("punch_in","punch_out") and sent[k] is not None else sent[k]
    if new["status"] in ("ABSENT","LEAVE"):
        if ("punch_in" in sent and sent["punch_in"] is not None) or ("punch_out" in sent and sent["punch_out"] is not None):
            raise HTTPException(422,"ABSENT/LEAVE cannot have punch times")
        new["punch_in"]=None; new["punch_out"]=None
    elif new["punch_in"] is None:
        raise HTTPException(422,"presence status requires punch_in")
    if new["punch_in"] is not None:
        if record_date(new["punch_in"],emp)!=att_date: raise HTTPException(422,"punch_in must belong to attendance date")
        new["late_minutes"]=late_minutes(new["punch_in"],att_date,emp)
    else: new["late_minutes"]=0
    if new["punch_out"] is not None:
        if new["punch_in"] is None or new["punch_out"]<=new["punch_in"] or (new["punch_out"]-new["punch_in"]).total_seconds()>86400:
            raise HTTPException(422,"punch_out must be after punch_in and within 24 hours")
        new["work_hours"]=calc_work_hours(new["punch_in"],new["punch_out"])
        new["overtime_minutes"]=overtime_minutes(new["punch_out"],att_date,emp)
        new["half_day"]=new["work_hours"]<4.50
    else:
        new["work_hours"]=None; new["overtime_minutes"]=0; new["half_day"]=False
    changes={}
    for k in old:
        a,b=old[k],new[k]
        if k in ("punch_in","punch_out") and a is not None and a.tzinfo is None: a=a.replace(tzinfo=UTC)
        if a!=b: changes[k]={"from":old[k],"to":new[k]}
    if not changes: raise HTTPException(422,"request makes no changes")
    hist={"at":now_utc(),"by":body.regularized_by,"reason":body.reason,"changes":changes}
    # Optimistic concurrency: update only if the current history still matches what was read.
    query={"_id":rec["_id"],"history":rec.get("history",[])}
    # Legacy records may not have history at all.
    if "history" not in rec: query["history"]={"$exists":False}
    update_doc={k:new[k] for k in new}
    result=logs.update_one(query,{"$set":update_doc,"$push":{"history":hist}})
    if result.modified_count!=1: raise HTTPException(409,"record changed concurrently; retry")
    rec.update(update_doc); rec.setdefault("history",[]).append(hist)
    return output_record(rec)

def month_filter(month):
    start,end=parse_month(month)
    return start.isoformat(),end.isoformat(),start,end

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code:str,month:str):
    emp=employees.find_one({"emp_code":emp_code},{"_id":0,"emp_code":1,"joined_on":1})
    if not emp: raise HTTPException(404,"unknown employee")
    s,e,sd,ed=month_filter(month)
    effective=max(sd,date.fromisoformat(emp["joined_on"]))
    working=max(0,sum(1 for i in range((ed-effective).days) if (effective+timedelta(days=i)).weekday()<5))
    pipe=[{"$match":{"emp_code":emp_code,"date":{"$gte":s,"$lt":e}}},
      {"$group":{"_id":None,"present_days":{"$sum":{"$cond":[{"$in":["$status",list(PRESENCE)]},{"$cond":[{"$eq":["$half_day",True]},0.5,1]},0]}},
       "leave_days":{"$sum":{"$cond":[{"$eq":["$status","LEAVE"]},1,0]}},
       "late_count":{"$sum":{"$cond":[{"$gt":[{"$ifNull":["$late_minutes",0]},0]},1,0]}},
       "total_late_minutes":{"$sum":{"$ifNull":["$late_minutes",0]}},
       "total_overtime_minutes":{"$sum":{"$ifNull":["$overtime_minutes",0]}}}}]
    agg=list(logs.aggregate(pipe))
    a=agg[0] if agg else {}
    present=float(a.get("present_days",0))
    pct=round_half_up(present/working*100,2) if working else None
    return {"emp_code":emp_code,"month":month,"working_days":working,"present_days":present,
      "leave_days":a.get("leave_days",0),"late_count":a.get("late_count",0),
      "total_late_minutes":a.get("total_late_minutes",0),"total_overtime_minutes":a.get("total_overtime_minutes",0),
      "attendance_pct":pct}

@app.get("/analytics/departments/summary")
def department_summary(month:str,department:Optional[str]=None):
    s,e,sd,ed=month_filter(month)
    match={"joined_on":{"$lt":e}}
    if department: match["department"]=department
    pipeline=[{"$match":match},{"$lookup":{"from":"attendance_logs","let":{"code":"$emp_code","joined":"$joined_on"},"pipeline":[
      {"$match":{"$expr":{"$and":[{"$eq":["$emp_code","$$code"]},{"$gte":["$date",s]},{"$lt":["$date",e]},{"$gte":["$date","$$joined"]}]}}},
      {"$project":{"status":1,"work_hours":1,"late_minutes":1,"overtime_minutes":1,"half_day":1}}
    ],"as":"logs"}},{"$group":{"_id":"$department","headcount":{"$sum":1},
      "present_days":{"$sum":{"$sum":{"$map":{"input":"$logs","as":"l","in":{"$cond":[{"$in":["$$l.status",list(PRESENCE)]},{"$cond":[{"$eq":["$$l.half_day",True]},0.5,1]},0]}}}}},
      "hours_sum":{"$sum":{"$sum":{"$map":{"input":"$logs","as":"l","in":{"$cond":[{"$and":[{"$in":["$$l.status",list(PRESENCE)]},{"$ne":[{"$ifNull":["$$l.work_hours",None]},None]}]},"$$l.work_hours",0]}}}}},
      "hours_count":{"$sum":{"$size":{"$filter":{"input":"$logs","as":"l","cond":{"$and":[{"$in":["$$l.status",list(PRESENCE)]},{"$ne":[{"$ifNull":["$$l.work_hours",None]},None]}]}}}}},
      "late_count":{"$sum":{"$size":{"$filter":{"input":"$logs","as":"l","cond":{"$gt":[{"$ifNull":["$$l.late_minutes",0]},0]}}}}},
      "total_late_minutes":{"$sum":{"$sum":{"$map":{"input":"$logs","as":"l","in":{"$ifNull":["$$l.late_minutes",0]}}}}},
      "leave_count":{"$sum":{"$size":{"$filter":{"input":"$logs","as":"l","cond":{"$eq":["$$l.status","LEAVE"]}}}}},
      "on_duty_count":{"$sum":{"$size":{"$filter":{"input":"$logs","as":"l","cond":{"$eq":["$$l.status","ON_DUTY"]}}}}}}},
      {"$project":{"_id":0,"department":"$_id","headcount":1,"present_days":1,"avg_work_hours":{"$cond":[{"$gt":["$hours_count",0]},{"$round":[{"$divide":["$hours_sum","$hours_count"]},2]},None]},"late_count":1,"total_late_minutes":1,"leave_count":1,"on_duty_count":1}},
      {"$sort":{"department":1}}]
    items=list(employees.aggregate(pipeline))
    # Employees who join after month-end are excluded; a department with no eligible headcount has no group.
    return {"month":month,"items":items}

@app.get("/analytics/leaderboard/late")
def late_leaderboard(month:str,limit:int=Query(10,ge=1,le=50),department:Optional[str]=None):
    s,e,_,_=month_filter(month)
    pipe=[{"$match":{"date":{"$gte":s,"$lt":e},"late_minutes":{"$gt":0}}},
      {"$group":{"_id":"$emp_code","total_late_minutes":{"$sum":"$late_minutes"},"late_count":{"$sum":1}}},
      {"$match":{"total_late_minutes":{"$gt":0}}},
      {"$lookup":{"from":"employees","localField":"_id","foreignField":"emp_code","as":"emp"}},
      {"$unwind":"$emp"}]
    if department: pipe.append({"$match":{"emp.department":department}})
    pipe += [{"$setWindowFields":{"sortBy":{"total_late_minutes":-1},"output":{"rank":{"$rank":{}}}}},
      {"$match":{"rank":{"$lte":limit}}},
      {"$project":{"_id":0,"rank":1,"emp_code":"$_id","name":"$emp.name","department":"$emp.department","total_late_minutes":1,"late_count":1}},
      {"$sort":{"total_late_minutes":-1,"emp_code":1}}]
    return {"month":month,"items":list(logs.aggregate(pipe))}

@app.get("/analytics/departments/{department}/trend")
def department_trend(department:str,from_date:str=Query(alias="from"),to_date:str=Query(alias="to")):
    start=parse_date(from_date); end=parse_date(to_date)
    if end<start or (end-start).days+1>92: raise HTTPException(422,"invalid date range; maximum 92 days")
    if not employees.find_one({"department":department}): raise HTTPException(404,"unknown department")
    # Aggregation performs daily grouping and gap filling in MongoDB.
    pipe=[{"$match":{"date":{"$gte":start.isoformat(),"$lte":end.isoformat()}}},
      {"$lookup":{"from":"employees","localField":"emp_code","foreignField":"emp_code","as":"emp"}},
      {"$unwind":"$emp"},{"$match":{"emp.department":department}},
      {"$group":{"_id":"$date","present_count":{"$sum":{"$cond":[{"$in":["$status",list(PRESENCE)]},{"$cond":[{"$eq":["$half_day",True]},0.5,1]},0]}},
        "late_count":{"$sum":{"$cond":[{"$gt":[{"$ifNull":["$late_minutes",0]},0]},1,0]}}}},
      {"$project":{"_id":0,"date":{"$dateFromString":{"dateString":"$_id"}},"present_count":1,"late_count":1}},
      {"$densify":{"field":"date","range":{"step":1,"unit":"day","bounds":[datetime.combine(start,time.min),datetime.combine(end+timedelta(days=1),time.min)]}}},
      {"$set":{"date":{"$dateToString":{"format":"%Y-%m-%d","date":"$date","timezone":"UTC"}},"present_count":{"$ifNull":["$present_count",0]},"late_count":{"$ifNull":["$late_count",0]}}},
      {"$lookup":{"from":"employees","let":{"day":"$date"},"pipeline":[{"$match":{"$expr":{"$and":[{"$eq":["$department",department]},{"$lte":["$joined_on","$$day"]}]}}},{"$count":"n"}],"as":"hc"}},
      {"$set":{"headcount":{"$ifNull":[{"$arrayElemAt":["$hc.n",0]},0]}}},
      {"$set":{
        "is_working_day":{"$in":[{"$dayOfWeek":{"$dateFromString":{"dateString":"$date"}}},[2,3,4,5,6]]},
        "attendance_rate":{"$cond":[{"$and":[{"$in":[{"$dayOfWeek":{"$dateFromString":{"dateString":"$date"}}},[2,3,4,5,6]]},{"$gt":["$headcount",0]}]},{"$round":[{"$divide":["$present_count","$headcount"]},4]},None]}}},
      {"$setWindowFields":{"sortBy":{"date":1},"output":{"moving_avg_7d":{"$avg":"$attendance_rate","window":{"documents":[-6,0]}}}}},
      {"$project":{"_id":0,"date":1,"is_working_day":1,"headcount":1,"present_count":1,"late_count":1,"attendance_rate":1,"moving_avg_7d":{"$round":["$moving_avg_7d",4]}}}]
    items=list(logs.aggregate(pipe,allowDiskUse=False))
    # $densify may not emit a row if the source has no records in range; seed the series via a bounded calendar pipeline.
    # This aggregation has a document for each requested date; no Python-side analytics loop.
    if not items:
        seedpipe=[{"$documents":[{"date":datetime.combine(start,time.min)}]},
          {"$densify":{"field":"date","range":{"step":1,"unit":"day","bounds":[datetime.combine(start,time.min),datetime.combine(end+timedelta(days=1),time.min)]}}},
          {"$project":{"_id":0,"date":{"$dateToString":{"format":"%Y-%m-%d","date":"$date"}},"present_count":{"$literal":0},"late_count":{"$literal":0}}},
          {"$lookup":{"from":"employees","let":{"day":"$date"},"pipeline":[{"$match":{"$expr":{"$and":[{"$eq":["$department",department]},{"$lte":["$joined_on","$$day"]}]}}},{"$count":"n"}],"as":"hc"}},
          {"$set":{"headcount":{"$ifNull":[{"$arrayElemAt":["$hc.n",0]},0]},"is_working_day":{"$in":[{"$dayOfWeek":{"$dateFromString":{"dateString":"$date"}}},[2,3,4,5,6]]}}},
          {"$set":{"attendance_rate":{"$cond":[{"$and":["$is_working_day",{"$gt":["$headcount",0]}]},0,None]}}},
          {"$setWindowFields":{"sortBy":{"date":1},"output":{"moving_avg_7d":{"$avg":"$attendance_rate","window":{"documents":[-6,0]}}}}},
          {"$project":{"_id":0,"date":1,"is_working_day":1,"headcount":1,"present_count":1,"late_count":1,"attendance_rate":1,"moving_avg_7d":{"$round":["$moving_avg_7d",4]}}}]
        items=list(db.aggregate(seedpipe))
    return {"department":department,"items":items}

def explain_pipeline(collection, pipeline):
    # MongoDB explain command returns the raw executionStats plan.
    return db.command({"explain":{"aggregate":collection,"pipeline":pipeline,"cursor":{},"allowDiskUse":False},"verbosity":"executionStats"})

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint:str,emp_code:Optional[str]=None,month:Optional[str]=None,department:Optional[str]=None,
                     limit:int=Query(10,ge=1,le=50),date_from:Optional[str]=None,date_to:Optional[str]=None,
                     status:Optional[str]=None,from_date:Optional[str]=Query(None,alias="from"),to_date:Optional[str]=Query(None,alias="to"),
                     page:int=Query(1,ge=1),page_size:int=Query(20,ge=1,le=100)):
    allowed={"attendance_list","employee_monthly","department_summary","late_leaderboard","department_trend"}
    if endpoint not in allowed: raise HTTPException(422,"unknown endpoint")
    if endpoint=="attendance_list":
        q={}
        if emp_code:q["emp_code"]=emp_code
        if date_from or date_to:
            q["date"]={}
            if date_from:q["date"]["$gte"]=validate_date_value(date_from)
            if date_to:q["date"]["$lte"]=validate_date_value(date_to)
        if status:q["status"]=status
        exp=db.command({"explain":{"find":"attendance_logs","filter":q,"sort":{"date":-1,"emp_code":1},"skip":(page-1)*page_size,"limit":page_size},"verbosity":"executionStats"})
        return {"endpoint":endpoint,"collection":"attendance_logs","explain":exp}
    if not month: raise HTTPException(422,"month is required")
    s,e,_,_=month_filter(month)
    if endpoint=="employee_monthly":
        if not emp_code: raise HTTPException(422,"emp_code is required")
        pipe=[{"$match":{"emp_code":emp_code,"date":{"$gte":s,"$lt":e}}},{"$group":{"_id":"$emp_code","count":{"$sum":1}}}]
        return {"endpoint":endpoint,"collection":"attendance_logs","explain":explain_pipeline("attendance_logs",pipe)}
    if endpoint=="department_summary":
        pipe=[{"$match":{"date":{"$gte":s,"$lt":e}}},{"$group":{"_id":"$emp_code","n":{"$sum":1}}}]
        return {"endpoint":endpoint,"collection":"attendance_logs","explain":explain_pipeline("attendance_logs",pipe)}
    if endpoint=="late_leaderboard":
        pipe=[{"$match":{"date":{"$gte":s,"$lt":e},"late_minutes":{"$gt":0}}},{"$group":{"_id":"$emp_code","total":{"$sum":"$late_minutes"}}}]
        return {"endpoint":endpoint,"collection":"attendance_logs","explain":explain_pipeline("attendance_logs",pipe)}
    if not department or not from_date or not to_date: raise HTTPException(422,"department, from and to are required")
    parse_date(from_date);parse_date(to_date)
    pipe=[{"$match":{"date":{"$gte":from_date,"$lte":to_date}}},{"$group":{"_id":"$date","n":{"$sum":1}}}]
    return {"endpoint":endpoint,"collection":"attendance_logs","explain":explain_pipeline("attendance_logs",pipe)}
