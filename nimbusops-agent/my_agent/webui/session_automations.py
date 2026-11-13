"""Session-scoped automation payloads for the embedded WebUI."""

from __future__ import annotations

import time
from typing import Any, Protocol

from my_agent.cron.types import CronJob, CronSchedule


class _CronServiceLike(Protocol):
    def list_jobs(self, *, include_disabled: bool = False) -> list[CronJob]: ...

    def add_job(
        self,
        name: str,
        schedule: CronSchedule,
        message: str,
        deliver: bool = False,
        channel: str | None = None,
        to: str | None = None,
        delete_after_run: bool = False,
        channel_meta: dict | None = None,
        session_key: str | None = None,
    ) -> CronJob: ...


def session_automations_payload(
    cron_service: _CronServiceLike | None,
    session_key: str,
) -> dict[str, Any]:
    """Return user-created automation jobs attached to a WebUI session."""
    jobs: list[CronJob] = []
    if cron_service is not None:
        all_jobs = cron_service.list_jobs(include_disabled=True)
        jobs = [job for job in all_jobs if _job_matches_session(job, session_key)]
    return {"jobs": [_serialize_job(job) for job in jobs]}


def create_session_automation_job(
    cron_service: _CronServiceLike,
    session_key: str,
    payload: Any,
) -> CronJob:
    """Create a user automation job attached to a WebUI session."""
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")

    name = str(payload.get("name") or "").strip()
    message = str(payload.get("message") or "").strip()
    raw_schedule = payload.get("schedule")
    if not name:
        raise ValueError("name is required")
    if not message:
        raise ValueError("message is required")
    if not isinstance(raw_schedule, dict):
        raise ValueError("schedule is required")

    schedule_kind = str(raw_schedule.get("kind") or "").strip()
    try:
        if schedule_kind == "at":
            at_ms = int(raw_schedule.get("at_ms") or 0)
            schedule = CronSchedule(kind="at", at_ms=at_ms)
            delete_after_run = True
        elif schedule_kind == "every":
            every_ms = int(raw_schedule.get("every_ms") or 0)
            schedule = CronSchedule(kind="every", every_ms=every_ms)
            delete_after_run = False
        elif schedule_kind == "cron":
            expr = str(raw_schedule.get("expr") or "").strip()
            tz = str(raw_schedule.get("tz") or "").strip() or None
            schedule = CronSchedule(kind="cron", expr=expr, tz=tz)
            delete_after_run = False
        else:
            raise ValueError("unsupported schedule kind")
    except (TypeError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc) == "unsupported schedule kind":
            raise
        raise ValueError("invalid schedule") from exc

    if schedule.kind == "at" and (not schedule.at_ms):
        raise ValueError("at_ms is required")
    if schedule.kind == "at" and schedule.at_ms <= int(time.time() * 1000):
        raise ValueError("at_ms must be in the future")
    if schedule.kind == "every" and (not schedule.every_ms or schedule.every_ms <= 0):
        raise ValueError("every_ms must be positive")
    if schedule.kind == "cron" and not schedule.expr:
        raise ValueError("cron expr is required")

    channel, chat_id = _split_session_key(session_key)
    return cron_service.add_job(
        name=name,
        schedule=schedule,
        message=message,
        deliver=True,
        channel=channel,
        to=chat_id,
        delete_after_run=delete_after_run,
        session_key=session_key,
    )


def _split_session_key(key: str) -> tuple[str, str]:
    if ":" not in key:
        return "websocket", key
    return key.split(":", 1)


def _job_matches_session(job: CronJob, session_key: str) -> bool:
    payload = job.payload
    if payload.kind != "agent_turn":
        return False
    if payload.session_key:
        return payload.session_key == session_key
    if payload.channel and payload.to:
        return f"{payload.channel}:{payload.to}" == session_key
    return False


def _serialize_job(job: CronJob) -> dict[str, Any]:
    return {
        "id": job.id,
        "name": job.name,
        "enabled": job.enabled,
        "schedule": {
            "kind": job.schedule.kind,
            "at_ms": job.schedule.at_ms,
            "every_ms": job.schedule.every_ms,
            "expr": job.schedule.expr,
            "tz": job.schedule.tz,
        },
        "payload": {
            "message": job.payload.message,
        },
        "state": {
            "next_run_at_ms": job.state.next_run_at_ms,
            "last_status": job.state.last_status,
        },
    }
