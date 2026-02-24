import json
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional, Any
from app.db import fetch_all, fetch_one, execute
from app.events import emit_event


router = APIRouter(prefix="/jobs", tags=["jobs"])

class JobCreate(BaseModel):
    goal: str
    meta: dict = {}

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
