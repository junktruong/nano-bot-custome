"""Cron service for scheduling agent tasks."""

import asyncio
import json
import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Coroutine

from loguru import logger

from nanobot.cron.types import CronJob, CronJobState, CronPayload, CronSchedule, CronStore
from nanobot.utils.timezone import get_rtc_zoneinfo


def _now_ms() -> int:
    return int(time.time() * 1000)


def _normalize_job_id(raw: str | None) -> str:
    text = (raw or "").strip().strip("`'\"")
    if not text:
        return ""
    # Accept strings like "id: abc12345" or "(abc12345)".
    m = re.search(r"([A-Za-z0-9][A-Za-z0-9_-]{3,127})", text)
    return (m.group(1) if m else text).strip()


def _compute_next_run(schedule: CronSchedule, now_ms: int) -> int | None:
    """Compute next run time in ms."""
    if schedule.kind == "at":
        return schedule.at_ms if schedule.at_ms and schedule.at_ms > now_ms else None
    
    if schedule.kind == "every":
        if not schedule.every_ms or schedule.every_ms <= 0:
            return None
        # Next interval from now
        return now_ms + schedule.every_ms
    
    if schedule.kind == "cron" and schedule.expr:
        try:
            from croniter import croniter
            from zoneinfo import ZoneInfo
            # Use caller-provided reference time for deterministic scheduling
            base_time = now_ms / 1000
            rtc_tz = get_rtc_zoneinfo()
            tz = ZoneInfo(schedule.tz) if schedule.tz else rtc_tz
            base_dt = datetime.fromtimestamp(base_time, tz=tz)
            cron = croniter(schedule.expr, base_dt)
            next_dt = cron.get_next(datetime)
            return int(next_dt.timestamp() * 1000)
        except Exception:
            return None
    
    return None


def _validate_schedule_for_add(schedule: CronSchedule) -> None:
    """Validate schedule fields that would otherwise create non-runnable jobs."""
    if schedule.tz and schedule.kind != "cron":
        raise ValueError("tz can only be used with cron schedules")

    if schedule.kind == "cron" and schedule.tz:
        try:
            from zoneinfo import ZoneInfo

            ZoneInfo(schedule.tz)
        except Exception:
            raise ValueError(f"unknown timezone '{schedule.tz}'") from None


class CronService:
    """Service for managing and executing scheduled jobs."""
    
    def __init__(
        self,
        store_path: Path,
        on_job: Callable[[CronJob], Coroutine[Any, Any, str | None]] | None = None
    ):
        self.store_path = store_path
        self.on_job = on_job  # Callback to execute job, returns response text
        self._store: CronStore | None = None
        self._store_mtime_ns: int | None = None
        self._timer_task: asyncio.Task | None = None
        self._running = False

    @staticmethod
    def _to_bool(value: str | None, default: bool) -> bool:
        raw = (value or "").strip().lower()
        if not raw:
            return default
        return raw in {"1", "true", "yes", "on"}

    @staticmethod
    def _file_mtime_ns(path: Path) -> int | None:
        try:
            return path.stat().st_mtime_ns
        except Exception:
            return None

    def _decode_store(self, data: dict[str, Any]) -> CronStore:
        jobs: list[CronJob] = []
        for j in data.get("jobs", []):
            jobs.append(CronJob(
                id=j["id"],
                name=j["name"],
                enabled=j.get("enabled", True),
                schedule=CronSchedule(
                    kind=j["schedule"]["kind"],
                    at_ms=j["schedule"].get("atMs"),
                    every_ms=j["schedule"].get("everyMs"),
                    expr=j["schedule"].get("expr"),
                    tz=j["schedule"].get("tz"),
                ),
                payload=CronPayload(
                    kind=j["payload"].get("kind", "agent_turn"),
                    message=j["payload"].get("message", ""),
                    deliver=j["payload"].get("deliver", False),
                    channel=j["payload"].get("channel"),
                    to=j["payload"].get("to"),
                ),
                state=CronJobState(
                    next_run_at_ms=j.get("state", {}).get("nextRunAtMs"),
                    last_run_at_ms=j.get("state", {}).get("lastRunAtMs"),
                    last_status=j.get("state", {}).get("lastStatus"),
                    last_error=j.get("state", {}).get("lastError"),
                ),
                created_at_ms=j.get("createdAtMs", 0),
                updated_at_ms=j.get("updatedAtMs", 0),
                delete_after_run=j.get("deleteAfterRun", False),
            ))
        return CronStore(jobs=jobs)

    def _read_store_from_disk(self) -> CronStore:
        if not self.store_path.exists():
            self._store_mtime_ns = None
            return CronStore()
        try:
            data = json.loads(self.store_path.read_text(encoding="utf-8"))
            store = self._decode_store(data)
            self._store_mtime_ns = self._file_mtime_ns(self.store_path)
            return store
        except Exception as e:
            logger.warning("Failed to load cron store: {}", e)
            self._store_mtime_ns = self._file_mtime_ns(self.store_path)
            return CronStore()

    def _reload_store_if_changed(self) -> None:
        if self._store is None:
            return
        current = self._file_mtime_ns(self.store_path)
        if current is None:
            # File removed externally: keep memory copy, do not erase jobs unexpectedly.
            return
        if self._store_mtime_ns is not None and current == self._store_mtime_ns:
            return
        self._store = self._read_store_from_disk()
        logger.info("Cron: reloaded store from disk (external update detected)")
    
    def _load_store(self) -> CronStore:
        """Load jobs from disk."""
        if self._store is None:
            self._store = self._read_store_from_disk()
        else:
            self._reload_store_if_changed()
        return self._store
    
    def _save_store(self) -> None:
        """Save jobs to disk."""
        if not self._store:
            return
        
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        
        data = {
            "version": self._store.version,
            "jobs": [
                {
                    "id": j.id,
                    "name": j.name,
                    "enabled": j.enabled,
                    "schedule": {
                        "kind": j.schedule.kind,
                        "atMs": j.schedule.at_ms,
                        "everyMs": j.schedule.every_ms,
                        "expr": j.schedule.expr,
                        "tz": j.schedule.tz,
                    },
                    "payload": {
                        "kind": j.payload.kind,
                        "message": j.payload.message,
                        "deliver": j.payload.deliver,
                        "channel": j.payload.channel,
                        "to": j.payload.to,
                    },
                    "state": {
                        "nextRunAtMs": j.state.next_run_at_ms,
                        "lastRunAtMs": j.state.last_run_at_ms,
                        "lastStatus": j.state.last_status,
                        "lastError": j.state.last_error,
                    },
                    "createdAtMs": j.created_at_ms,
                    "updatedAtMs": j.updated_at_ms,
                    "deleteAfterRun": j.delete_after_run,
                }
                for j in self._store.jobs
            ]
        }
        
        self.store_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        self._store_mtime_ns = self._file_mtime_ns(self.store_path)
    
    async def start(self) -> None:
        """Start the cron service."""
        self._running = True
        self._load_store()
        if self._to_bool(os.environ.get("NANOBOT_CRON_CATCHUP_ON_START"), True):
            await self._run_startup_catchup()
        self._recompute_next_runs()
        self._save_store()
        self._arm_timer()
        logger.info(
            "Cron service started with {} jobs (store={})",
            len(self._store.jobs if self._store else []),
            self.store_path,
        )
    
    def stop(self) -> None:
        """Stop the cron service."""
        self._running = False
        if self._timer_task:
            self._timer_task.cancel()
            self._timer_task = None
    
    def _recompute_next_runs(self) -> None:
        """Recompute next run times for all enabled jobs."""
        if not self._store:
            return
        now = _now_ms()
        for job in self._store.jobs:
            if job.enabled:
                job.state.next_run_at_ms = _compute_next_run(job.schedule, now)
    
    def _get_next_wake_ms(self) -> int | None:
        """Get the earliest next run time across all jobs."""
        if not self._store:
            return None
        times = [j.state.next_run_at_ms for j in self._store.jobs 
                 if j.enabled and j.state.next_run_at_ms]
        return min(times) if times else None
    
    def _arm_timer(self) -> None:
        """Schedule the next timer tick."""
        if self._timer_task:
            self._timer_task.cancel()
        
        next_wake = self._get_next_wake_ms()
        if not next_wake or not self._running:
            return
        
        delay_ms = max(0, next_wake - _now_ms())
        delay_s = delay_ms / 1000
        
        async def tick():
            await asyncio.sleep(delay_s)
            if self._running:
                await self._on_timer()
        
        self._timer_task = asyncio.create_task(tick())
    
    async def _on_timer(self) -> None:
        """Handle timer tick - run due jobs."""
        store = self._load_store()
        if not store:
            return
        
        now = _now_ms()
        due_jobs = [
            j for j in store.jobs
            if j.enabled and j.state.next_run_at_ms and now >= j.state.next_run_at_ms
        ]
        
        for job in due_jobs:
            await self._execute_job(job)
        
        self._save_store()
        self._arm_timer()
    
    async def _execute_job(self, job: CronJob) -> None:
        """Execute a single job."""
        start_ms = _now_ms()
        logger.info("Cron: executing job '{}' ({})", job.name, job.id)
        
        try:
            response = None
            if self.on_job:
                response = await self.on_job(job)
            
            job.state.last_status = "ok"
            job.state.last_error = None
            logger.info("Cron: job '{}' completed", job.name)
            
        except Exception as e:
            job.state.last_status = "error"
            job.state.last_error = str(e)
            logger.error("Cron: job '{}' failed: {}", job.name, e)
        
        job.state.last_run_at_ms = start_ms
        job.updated_at_ms = _now_ms()
        
        # Handle one-shot jobs
        if job.schedule.kind == "at":
            if job.delete_after_run:
                self._store.jobs = [j for j in self._store.jobs if j.id != job.id]
            else:
                job.enabled = False
                job.state.next_run_at_ms = None
        else:
            # Compute next run
            job.state.next_run_at_ms = _compute_next_run(job.schedule, _now_ms())

    async def _run_startup_catchup(self) -> None:
        """Run one catch-up execution for missed cron/at jobs while bot was offline."""
        store = self._load_store()
        now = _now_ms()
        due: list[CronJob] = []
        for job in store.jobs:
            if not job.enabled:
                continue
            if self._job_missed(job, now):
                due.append(job)

        if not due:
            return

        logger.info("Cron startup catch-up: {} job(s) due from offline period", len(due))
        for job in due:
            await self._execute_job(job)

    @staticmethod
    def _job_missed(job: CronJob, now_ms: int) -> bool:
        """Return True if job has at least one missed occurrence since last run."""
        last_run = int(job.state.last_run_at_ms or 0)

        if job.schedule.kind == "at":
            at_ms = int(job.schedule.at_ms or 0)
            return at_ms > 0 and at_ms <= now_ms and last_run < at_ms

        if job.schedule.kind != "cron" or not job.schedule.expr:
            return False

        try:
            from croniter import croniter
            from zoneinfo import ZoneInfo

            tz = ZoneInfo(job.schedule.tz) if job.schedule.tz else get_rtc_zoneinfo()
            base_dt = datetime.fromtimestamp(now_ms / 1000, tz=tz)
            prev_dt = croniter(job.schedule.expr, base_dt).get_prev(datetime)
            prev_ms = int(prev_dt.timestamp() * 1000)
            return prev_ms > last_run
        except Exception:
            return False
    
    # ========== Public API ==========
    
    def list_jobs(self, include_disabled: bool = False) -> list[CronJob]:
        """List all jobs."""
        store = self._load_store()
        jobs = store.jobs if include_disabled else [j for j in store.jobs if j.enabled]
        return sorted(jobs, key=lambda j: j.state.next_run_at_ms or float('inf'))
    
    def add_job(
        self,
        name: str,
        schedule: CronSchedule,
        message: str,
        deliver: bool = False,
        channel: str | None = None,
        to: str | None = None,
        delete_after_run: bool = False,
    ) -> CronJob:
        """Add a new job."""
        store = self._load_store()
        _validate_schedule_for_add(schedule)
        now = _now_ms()
        
        job = CronJob(
            id=str(uuid.uuid4())[:8],
            name=name,
            enabled=True,
            schedule=schedule,
            payload=CronPayload(
                kind="agent_turn",
                message=message,
                deliver=deliver,
                channel=channel,
                to=to,
            ),
            state=CronJobState(next_run_at_ms=_compute_next_run(schedule, now)),
            created_at_ms=now,
            updated_at_ms=now,
            delete_after_run=delete_after_run,
        )
        
        store.jobs.append(job)
        self._save_store()
        self._arm_timer()
        
        logger.info("Cron: added job '{}' ({})", name, job.id)
        return job
    
    def remove_job(self, job_id: str) -> bool:
        """Remove a job by ID."""
        store = self._load_store()
        resolved = self._resolve_job_id(job_id, store=store)
        if not resolved:
            return False
        before = len(store.jobs)
        store.jobs = [j for j in store.jobs if j.id != resolved]
        removed = len(store.jobs) < before
        
        if removed:
            self._save_store()
            self._arm_timer()
            logger.info("Cron: removed job {}", resolved)
        
        return removed

    def clear_jobs(self) -> int:
        """Remove all jobs and return number of removed jobs."""
        store = self._load_store()
        removed = len(store.jobs)
        if removed <= 0:
            return 0
        store.jobs = []
        self._save_store()
        self._arm_timer()
        logger.info("Cron: cleared {} jobs", removed)
        return removed

    def get_job(self, job_id: str) -> CronJob | None:
        """Get a job by ID."""
        store = self._load_store()
        resolved = self._resolve_job_id(job_id, store=store)
        if not resolved:
            return None
        for job in store.jobs:
            if job.id == resolved:
                return job
        return None

    def update_job(
        self,
        job_id: str,
        *,
        name: str | None = None,
        message: str | None = None,
        schedule: CronSchedule | None = None,
        enabled: bool | None = None,
        delete_after_run: bool | None = None,
    ) -> CronJob | None:
        """Update mutable fields of an existing job."""
        store = self._load_store()
        resolved = self._resolve_job_id(job_id, store=store)
        if not resolved:
            return None
        for job in store.jobs:
            if job.id != resolved:
                continue

            if name is not None:
                job.name = name
            if message is not None:
                job.payload.message = message
            if schedule is not None:
                _validate_schedule_for_add(schedule)
                job.schedule = schedule
            if enabled is not None:
                job.enabled = enabled
            if delete_after_run is not None:
                job.delete_after_run = delete_after_run

            job.updated_at_ms = _now_ms()
            if job.enabled:
                job.state.next_run_at_ms = _compute_next_run(job.schedule, _now_ms())
            else:
                job.state.next_run_at_ms = None

            self._save_store()
            self._arm_timer()
            logger.info("Cron: updated job {}", job.id)
            return job
        return None
    
    def enable_job(self, job_id: str, enabled: bool = True) -> CronJob | None:
        """Enable or disable a job."""
        store = self._load_store()
        resolved = self._resolve_job_id(job_id, store=store)
        if not resolved:
            return None
        for job in store.jobs:
            if job.id == resolved:
                job.enabled = enabled
                job.updated_at_ms = _now_ms()
                if enabled:
                    job.state.next_run_at_ms = _compute_next_run(job.schedule, _now_ms())
                else:
                    job.state.next_run_at_ms = None
                self._save_store()
                self._arm_timer()
                return job
        return None
    
    async def run_job(self, job_id: str, force: bool = False) -> bool:
        """Manually run a job."""
        store = self._load_store()
        resolved = self._resolve_job_id(job_id, store=store)
        if not resolved:
            return False
        for job in store.jobs:
            if job.id == resolved:
                if not force and not job.enabled:
                    return False
                await self._execute_job(job)
                self._save_store()
                self._arm_timer()
                return True
        return False

    @staticmethod
    def _resolve_job_id(job_id: str | None, store: CronStore) -> str | None:
        target = _normalize_job_id(job_id)
        if not target:
            return None
        lower = target.lower()

        exact = [j.id for j in store.jobs if j.id.lower() == lower]
        if exact:
            return exact[0]

        prefixed = [j.id for j in store.jobs if j.id.lower().startswith(lower)]
        if len(prefixed) == 1:
            return prefixed[0]
        return None
    
    def status(self) -> dict:
        """Get service status."""
        store = self._load_store()
        return {
            "enabled": self._running,
            "jobs": len(store.jobs),
            "next_wake_at_ms": self._get_next_wake_ms(),
        }
