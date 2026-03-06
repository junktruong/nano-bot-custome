"""Cron tool for scheduling reminders and tasks."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from nanobot.agent.tools.base import Tool
from nanobot.cron.service import CronService
from nanobot.cron.types import CronJob, CronSchedule
from nanobot.utils.timezone import get_rtc_timezone_name


class CronTool(Tool):
    """Tool to schedule reminders and recurring tasks."""

    def __init__(self, cron_service: CronService):
        self._cron = cron_service
        self._channel = ""
        self._chat_id = ""

    def set_context(self, channel: str, chat_id: str) -> None:
        """Set the current session context for delivery."""
        self._channel = channel
        self._chat_id = chat_id

    @property
    def name(self) -> str:
        return "cron"

    @property
    def description(self) -> str:
        return (
            "Schedule reminders and recurring tasks. "
            "Actions: add, list, remove, delete, update, edit, enable, disable, clear, reset."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "add", "list", "remove", "delete", "update", "edit",
                        "enable", "disable", "clear", "reset", "remove_all",
                    ],
                    "description": "Action to perform",
                },
                "job_id": {
                    "type": "string",
                    "description": "Job ID (required for remove/update/enable/disable). Use 'all' to clear all.",
                },
                "name": {
                    "type": "string",
                    "description": "Job name (optional for add/update)",
                },
                "message": {
                    "type": "string",
                    "description": "Reminder content (required for add, optional for update)",
                },
                "every_seconds": {
                    "type": "integer",
                    "description": "Run every N seconds (recurring)",
                },
                "every_hours": {
                    "type": "integer",
                    "description": "Run every N hours (recurring)",
                },
                "cron_expr": {
                    "type": "string",
                    "description": "Cron expression like '0 9 * * *'",
                },
                "hour": {
                    "type": "integer",
                    "description": "Hour (0-23) for daily/weekly cron generation",
                },
                "minute": {
                    "type": "integer",
                    "description": "Minute (0-59), default 0",
                },
                "weekdays": {
                    "type": "string",
                    "description": (
                        "Weekday set for weekly cron, e.g. 'mon,wed,fri', '1-5', 'thu2,thu4', or 'cn'"
                    ),
                },
                "tz": {
                    "type": "string",
                    "description": "IANA timezone for cron expressions (e.g. 'Asia/Ho_Chi_Minh')",
                },
                "at": {
                    "type": "string",
                    "description": "ISO datetime for one-time execution (e.g. '2026-03-03T14:20:00')",
                },
                "period": {
                    "type": "string",
                    "enum": ["all", "day", "week", "month"],
                    "description": "List scope when action='list'",
                },
                "date": {
                    "type": "string",
                    "description": "Anchor date for list period, format YYYY-MM-DD (default today)",
                },
                "include_disabled": {
                    "type": "boolean",
                    "description": "Include disabled jobs in list",
                },
            },
            "required": ["action"],
        }

    async def execute(
        self,
        action: str,
        job_id: str | None = None,
        name: str | None = None,
        message: str = "",
        every_seconds: int | None = None,
        every_hours: int | None = None,
        cron_expr: str | None = None,
        hour: int | None = None,
        minute: int | None = None,
        weekdays: str | None = None,
        tz: str | None = None,
        at: str | None = None,
        period: str = "all",
        date: str | None = None,
        include_disabled: bool = False,
        **kwargs: Any,
    ) -> str:
        del kwargs
        act = (action or "").strip().lower()

        if act == "add":
            return self._add_job(
                message=message,
                name=name,
                every_seconds=every_seconds,
                every_hours=every_hours,
                cron_expr=cron_expr,
                hour=hour,
                minute=minute,
                weekdays=weekdays,
                tz=tz,
                at=at,
            )
        if act == "list":
            return self._list_jobs(period=period, date=date, tz=tz, include_disabled=include_disabled)
        if act in {"remove", "delete"}:
            return self._remove_job(job_id)
        if act in {"clear", "reset", "remove_all"}:
            return self._clear_jobs()
        if act in {"update", "edit"}:
            return self._update_job(
                job_id=job_id,
                name=name,
                message=message,
                every_seconds=every_seconds,
                every_hours=every_hours,
                cron_expr=cron_expr,
                hour=hour,
                minute=minute,
                weekdays=weekdays,
                tz=tz,
                at=at,
            )
        if act == "enable":
            return self._enable_disable(job_id=job_id, enabled=True)
        if act == "disable":
            return self._enable_disable(job_id=job_id, enabled=False)
        return f"Unknown action: {action}"

    def _add_job(
        self,
        message: str,
        name: str | None,
        every_seconds: int | None,
        every_hours: int | None,
        cron_expr: str | None,
        hour: int | None,
        minute: int | None,
        weekdays: str | None,
        tz: str | None,
        at: str | None,
    ) -> str:
        if not message:
            return "Error: message is required for add"
        if not self._channel or not self._chat_id:
            return "Error: no session context (channel/chat_id)"

        schedule, delete_after = self._build_schedule(
            every_seconds=every_seconds,
            every_hours=every_hours,
            cron_expr=cron_expr,
            hour=hour,
            minute=minute,
            weekdays=weekdays,
            tz=tz,
            at=at,
        )
        if isinstance(schedule, str):
            return schedule

        job = self._cron.add_job(
            name=(name or message[:48]),
            schedule=schedule,
            message=message,
            deliver=True,
            channel=self._channel,
            to=self._chat_id,
            delete_after_run=delete_after,
        )
        return (
            f"Created job '{job.name}' (id: {job.id})\n"
            f"Schedule: {self._format_schedule(job)}\n"
            f"Next run: {self._format_next_run(job)}"
        )

    def _update_job(
        self,
        job_id: str | None,
        name: str | None,
        message: str,
        every_seconds: int | None,
        every_hours: int | None,
        cron_expr: str | None,
        hour: int | None,
        minute: int | None,
        weekdays: str | None,
        tz: str | None,
        at: str | None,
    ) -> str:
        if not job_id:
            return "Error: job_id is required for update"

        has_schedule_change = any(
            v is not None for v in (every_seconds, every_hours, cron_expr, hour, minute, weekdays, at)
        )
        if not has_schedule_change and name is None and not message:
            return "Error: provide at least one field to update (name/message/schedule)"

        schedule: CronSchedule | None = None
        delete_after_run: bool | None = None
        if has_schedule_change:
            built, delete_after = self._build_schedule(
                every_seconds=every_seconds,
                every_hours=every_hours,
                cron_expr=cron_expr,
                hour=hour,
                minute=minute,
                weekdays=weekdays,
                tz=tz,
                at=at,
            )
            if isinstance(built, str):
                return built
            schedule = built
            delete_after_run = delete_after

        job = self._cron.update_job(
            job_id=job_id,
            name=name,
            message=(message if message else None),
            schedule=schedule,
            delete_after_run=delete_after_run,
        )
        if not job:
            return f"Job {job_id} not found"
        return (
            f"Updated job '{job.name}' (id: {job.id})\n"
            f"Schedule: {self._format_schedule(job)}\n"
            f"Next run: {self._format_next_run(job)}"
        )

    def _remove_job(self, job_id: str | None) -> str:
        if not job_id:
            return "Error: job_id is required for remove"
        if (job_id or "").strip().lower() in {"all", "*", "tatca", "toanbo"}:
            return self._clear_jobs()
        if self._cron.remove_job(job_id):
            return f"Removed job {job_id}"
        return f"Job {job_id} not found"

    def _clear_jobs(self) -> str:
        removed = self._cron.clear_jobs()
        if removed <= 0:
            return "No scheduled jobs to clear."
        return f"Cleared {removed} scheduled job(s)."

    def _enable_disable(self, job_id: str | None, enabled: bool) -> str:
        if not job_id:
            return "Error: job_id is required"
        job = self._cron.enable_job(job_id, enabled=enabled)
        if not job:
            return f"Job {job_id} not found"
        if enabled:
            return f"Enabled job {job.id}. Next run: {self._format_next_run(job)}"
        return f"Disabled job {job.id}"

    def _list_jobs(
        self,
        period: str,
        date: str | None,
        tz: str | None,
        include_disabled: bool,
    ) -> str:
        p = (period or "all").strip().lower()
        if p not in {"all", "day", "week", "month"}:
            return "Error: period must be one of: all, day, week, month"

        tzinfo_or_err = self._resolve_tz(tz)
        if isinstance(tzinfo_or_err, str):
            return tzinfo_or_err
        tzinfo = tzinfo_or_err

        jobs = self._cron.list_jobs(include_disabled=include_disabled)
        if p == "all":
            if not jobs:
                return "No scheduled jobs."
            lines = [self._format_job_line(j) for j in jobs]
            return "Scheduled jobs:\n" + "\n".join(lines)

        bounds = self._period_bounds(period=p, date=date, tzinfo=tzinfo)
        if isinstance(bounds, str):
            return bounds
        start_ms, end_ms, label = bounds

        matched: list[CronJob] = []
        for job in jobs:
            ts = job.state.next_run_at_ms
            if ts is None:
                continue
            if start_ms <= ts < end_ms:
                matched.append(job)

        if not matched:
            return f"No scheduled jobs in {label} (tz={tzinfo.key})."

        lines = [self._format_job_line(j) for j in matched]
        return f"Scheduled jobs in {label} (tz={tzinfo.key}):\n" + "\n".join(lines)

    def _build_schedule(
        self,
        every_seconds: int | None,
        every_hours: int | None,
        cron_expr: str | None,
        hour: int | None,
        minute: int | None,
        weekdays: str | None,
        tz: str | None,
        at: str | None,
    ) -> tuple[CronSchedule | str, bool]:
        spec_count = 0
        if at:
            spec_count += 1
        if cron_expr:
            spec_count += 1
        if every_seconds is not None or every_hours is not None:
            spec_count += 1
        if hour is not None or minute is not None or weekdays:
            spec_count += 1
        if spec_count == 0:
            return "Error: missing schedule. Use at/cron_expr/every_seconds/every_hours/hour.", False
        if spec_count > 1:
            return (
                "Error: ambiguous schedule. Provide only one of: "
                "at, cron_expr, every_*, or hour/minute/weekdays.",
                False,
            )

        tzinfo_or_err = self._resolve_tz(tz) if tz else None
        if isinstance(tzinfo_or_err, str):
            return tzinfo_or_err, False

        if at:
            try:
                dt = datetime.fromisoformat(at)
            except Exception:
                return "Error: invalid 'at' datetime. Use ISO format like 2026-03-03T10:20:00", False
            if dt.tzinfo is None:
                tz_for_at = self._resolve_tz(tz)
                if isinstance(tz_for_at, str):
                    return tz_for_at, False
                dt = dt.replace(tzinfo=tz_for_at)
            at_ms = int(dt.timestamp() * 1000)
            return CronSchedule(kind="at", at_ms=at_ms), True

        if every_seconds is not None or every_hours is not None:
            if every_seconds is not None and every_hours is not None:
                return "Error: use only one of every_seconds or every_hours", False
            sec = every_seconds if every_seconds is not None else int(every_hours or 0) * 3600
            if sec <= 0:
                return "Error: every_seconds/every_hours must be > 0", False
            return CronSchedule(kind="every", every_ms=sec * 1000), False

        if cron_expr:
            return CronSchedule(kind="cron", expr=cron_expr.strip(), tz=tz), False

        # hour/minute/weekdays convenience path
        if hour is None:
            return "Error: hour is required when using minute/weekdays convenience fields", False
        if hour < 0 or hour > 23:
            return "Error: hour must be in range 0..23", False
        mm = 0 if minute is None else minute
        if mm < 0 or mm > 59:
            return "Error: minute must be in range 0..59", False
        dow = "*" if not weekdays else self._parse_weekdays(weekdays)
        if dow.startswith("Error:"):
            return dow, False
        expr = f"{mm} {hour} * * {dow}"
        return CronSchedule(kind="cron", expr=expr, tz=tz), False

    @staticmethod
    def _parse_weekdays(raw: str) -> str:
        text = (raw or "").strip().lower()
        if not text:
            return "*"
        if re.fullmatch(r"[0-7,\-*/]+", text):
            return text

        mapping = {
            "mon": 1, "monday": 1, "t2": 1, "thu2": 1,
            "tue": 2, "tuesday": 2, "t3": 2, "thu3": 2,
            "wed": 3, "wednesday": 3, "t4": 3, "thu4": 3,
            "thu": 4, "thursday": 4, "t5": 4, "thu5": 4,
            "fri": 5, "friday": 5, "t6": 5, "thu6": 5,
            "sat": 6, "saturday": 6, "t7": 6, "thu7": 6,
            "sun": 0, "sunday": 0, "cn": 0, "chunhat": 0,
        }

        values: set[int] = set()
        for token in re.split(r"[\s,;]+", text):
            tok = token.strip()
            if not tok:
                continue
            if tok in mapping:
                values.add(mapping[tok])
                continue
            if tok.isdigit():
                n = int(tok)
                if n < 0 or n > 7:
                    return "Error: weekdays numeric values must be in range 0..7"
                values.add(0 if n == 7 else n)
                continue
            return (
                "Error: invalid weekdays. Use e.g. 'mon,wed,fri', '1-5', "
                "'thu2,thu4', or 'cn'"
            )
        if not values:
            return "Error: weekdays is empty"
        ordered = sorted(values, key=lambda x: (x == 0, x))
        return ",".join(str(v) for v in ordered)

    @staticmethod
    def _default_rtc_tz_name() -> str:
        return get_rtc_timezone_name()

    @classmethod
    def _resolve_tz(cls, tz: str | None) -> ZoneInfo | str:
        zone = (tz or "").strip() or cls._default_rtc_tz_name()
        try:
            return ZoneInfo(zone)
        except Exception:
            return f"Error: unknown timezone '{zone}'"

    def _period_bounds(
        self,
        period: str,
        date: str | None,
        tzinfo: ZoneInfo,
    ) -> tuple[int, int, str] | str:
        ref: datetime
        if date:
            try:
                if len(date) == 10:
                    ref = datetime.fromisoformat(f"{date}T00:00:00")
                else:
                    ref = datetime.fromisoformat(date)
            except Exception:
                return "Error: invalid date. Use YYYY-MM-DD or ISO datetime."
        else:
            ref = datetime.now(tzinfo)

        if ref.tzinfo is None:
            ref = ref.replace(tzinfo=tzinfo)
        else:
            ref = ref.astimezone(tzinfo)

        if period == "day":
            start = ref.replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + timedelta(days=1)
            label = f"day {start.date().isoformat()}"
        elif period == "week":
            start = ref.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=ref.weekday())
            end = start + timedelta(days=7)
            label = f"week {start.date().isoformat()}..{(end - timedelta(days=1)).date().isoformat()}"
        elif period == "month":
            start = ref.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            if start.month == 12:
                end = start.replace(year=start.year + 1, month=1)
            else:
                end = start.replace(month=start.month + 1)
            label = f"month {start.strftime('%Y-%m')}"
        else:
            return "Error: unsupported period"

        return int(start.timestamp() * 1000), int(end.timestamp() * 1000), label

    def _format_job_line(self, job: CronJob) -> str:
        status = "enabled" if job.enabled else "disabled"
        return (
            f"- {job.name} (id: {job.id}, {status}) | "
            f"{self._format_schedule(job)} | next: {self._format_next_run(job)}"
        )

    def _format_schedule(self, job: CronJob) -> str:
        s = job.schedule
        if s.kind == "every":
            sec = int((s.every_ms or 0) / 1000)
            if sec % 3600 == 0 and sec >= 3600:
                return f"every {sec // 3600}h"
            if sec % 60 == 0 and sec >= 60:
                return f"every {sec // 60}m"
            return f"every {sec}s"
        if s.kind == "cron":
            return f"cron '{s.expr}' ({s.tz or self._default_rtc_tz_name()})"
        if s.kind == "at":
            if s.at_ms is None:
                return "at <invalid>"
            dt = datetime.fromtimestamp(s.at_ms / 1000).strftime("%Y-%m-%d %H:%M")
            return f"at {dt}"
        return s.kind

    def _format_next_run(self, job: CronJob) -> str:
        ts = job.state.next_run_at_ms
        if ts is None:
            return "-"
        try:
            tz = ZoneInfo(job.schedule.tz) if job.schedule.tz else ZoneInfo(self._default_rtc_tz_name())
            return datetime.fromtimestamp(ts / 1000, tz=tz).strftime("%Y-%m-%d %H:%M:%S %Z")
        except Exception:
            return datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M:%S")
