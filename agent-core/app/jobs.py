import json
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional, Any, List
from app.db import fetch_all, fetch_one, execute
from app.events import emit_event


router = APIRouter(prefix="/jobs", tags=["jobs"])

class JobCreate(BaseModel):
    goal: str
    meta: dict = {}

class PlanStep(BaseModel):
    type: str  # ai | shell | code
    goal: str
    meta: dict = {}

class PlanCreate(BaseModel):
    steps: list[PlanStep]
    plan_name: Optional[str] = None


@router.post("/create")
def create_job(job: JobCreate):
    row = fetch_one(
        """INSERT INTO jobs (goal, meta)
           VALUES (%s, %s::jsonb)
           RETURNING *""",
            (job.goal, json.dumps(job.meta))
    )
    emit_event("job_created", {"job_id": row["id"]})
    return row

@router.post("/plan")
def create_plan(plan: PlanCreate):
    if not plan.steps:
        raise HTTPException(422, detail="steps empty")

    plan_id = uuid4().hex[:8]
    job_ids = []

    for i, step in enumerate(plan.steps):
        t = (step.type or "").strip().lower()
        if t not in ("ai", "shell", "code"):
            raise HTTPException(422, detail=f"invalid step.type: {step.type}")

        meta = dict(step.meta or {})
        meta.update({"type": t, "plan_id": plan_id, "step_index": i})
        if plan.plan_name:
            meta["plan_name"] = plan.plan_name

        row = fetch_one(
            """INSERT INTO jobs (goal, meta)
               VALUES (%s, %s::jsonb)
               RETURNING *""",
            (step.goal, json.dumps(meta))
        )
        job_ids.append(row["id"])
        emit_event("job_created", {"job_id": row["id"], "plan_id": plan_id, "step_index": i})

    emit_event("plan_created", {"plan_id": plan_id, "job_ids": job_ids, "plan_name": plan.plan_name})
    return {"plan_id": plan_id, "job_ids": job_ids}


@router.get("")
def list_jobs():
    return fetch_all("SELECT * FROM jobs ORDER BY id DESC LIMIT 50")

@router.get("/{job_id}")
def get_job(job_id: int):
    row = fetch_one("SELECT * FROM jobs WHERE id=%s", (job_id,))
    if not row:
        raise HTTPException(404)
    return row

class JobUpdate(BaseModel):
    status: Optional[str] = None
    log: Optional[Any] = None
    result: Optional[dict] = None

@router.post("/{job_id}/update")
def update_job(job_id: int, data: JobUpdate):
    if data.status:
        execute("UPDATE jobs SET status=%s WHERE id=%s", (data.status, job_id))
        emit_event(f"job_{data.status}", {"job_id": job_id})

    if data.log is not None:
        execute(
            "UPDATE jobs SET logs = logs || %s::jsonb WHERE id=%s",
            (json.dumps([data.log]), job_id)
        )

    if data.result is not None:
        execute(
            "UPDATE jobs SET result = COALESCE(result, '{}'::jsonb) || %s::jsonb WHERE id=%s",
            (json.dumps(data.result), job_id)
        )

    return fetch_one("SELECT * FROM jobs WHERE id=%s", (job_id,))
