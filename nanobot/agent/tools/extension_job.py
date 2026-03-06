"""Tool for delegating long-running work to an external extension worker."""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

import httpx

from nanobot.agent.tools.base import Tool


class ExtensionJobTool(Tool):
    """Submit/poll/cancel jobs on an external worker service."""

    def __init__(
        self,
        base_url: str,
        api_token: str = "",
        timeout_seconds: int = 30,
        poll_interval_seconds: int = 3,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_token = api_token.strip()
        self.timeout_seconds = max(5, int(timeout_seconds))
        self.poll_interval_seconds = max(1, int(poll_interval_seconds))

    @classmethod
    def from_env(cls) -> "ExtensionJobTool | None":
        base_url = (os.environ.get("NANOBOT_EXTENSION_BASE_URL") or "").strip() or "http://127.0.0.1:7091"
        token = os.environ.get("NANOBOT_EXTENSION_TOKEN", "")
        timeout = int(os.environ.get("NANOBOT_EXTENSION_TIMEOUT_SECONDS", "30"))
        poll = int(os.environ.get("NANOBOT_EXTENSION_POLL_SECONDS", "3"))
        return cls(base_url=base_url, api_token=token, timeout_seconds=timeout, poll_interval_seconds=poll)

    @property
    def name(self) -> str:
        return "extension_job"

    @property
    def description(self) -> str:
        return (
            "Delegate task execution to external extension worker and monitor progress. "
            "Actions: submit, status, result, wait, cancel."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["submit", "status", "result", "wait", "cancel"],
                    "description": "Action to perform",
                },
                "job_id": {
                    "type": "string",
                    "description": "Existing job id (required for status/result/wait/cancel)",
                },
                "task_type": {
                    "type": "string",
                    "description": (
                        "Task type for submit (e.g. google_docs_create_cli, "
                        "google_sheet_append_cli, google_docs_create_web)"
                    ),
                },
                "payload": {
                    "type": "object",
                    "description": "Task payload for submit",
                },
                "timeout_seconds": {
                    "type": "integer",
                    "description": "Override wait timeout (for action=wait)",
                },
                "poll_seconds": {
                    "type": "integer",
                    "description": "Override poll interval (for action=wait)",
                },
            },
            "required": ["action"],
        }

    async def execute(
        self,
        action: str,
        job_id: str | None = None,
        task_type: str | None = None,
        payload: dict[str, Any] | None = None,
        timeout_seconds: int | None = None,
        poll_seconds: int | None = None,
        **kwargs: Any,
    ) -> str:
        del kwargs
        action = (action or "").strip().lower()
        headers = {"Content-Type": "application/json"}
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"

        if action == "submit":
            if not task_type:
                return "Error: task_type is required for submit"
            body = {"task_type": task_type, "payload": payload or {}}
            return await self._request("POST", "/jobs", headers=headers, json_body=body)

        if action in {"status", "result", "cancel", "wait"} and not job_id:
            return f"Error: job_id is required for {action}"

        if action == "status":
            return await self._request("GET", f"/jobs/{job_id}", headers=headers)
        if action == "result":
            return await self._request("GET", f"/jobs/{job_id}/result", headers=headers)
        if action == "cancel":
            return await self._request("POST", f"/jobs/{job_id}/cancel", headers=headers, json_body={})
        if action == "wait":
            return await self._wait_job(
                job_id=job_id or "",
                headers=headers,
                timeout_seconds=timeout_seconds or self.timeout_seconds,
                poll_seconds=poll_seconds or self.poll_interval_seconds,
            )

        return f"Error: unknown action '{action}'"

    async def _request(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        json_body: dict[str, Any] | None = None,
    ) -> str:
        url = f"{self.base_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                resp = await client.request(method=method, url=url, headers=headers, json=json_body)
                body_text = resp.text
                if not resp.is_success:
                    return f"Error: extension {method} {path} -> {resp.status_code}: {body_text[:500]}"
                try:
                    data = resp.json()
                    return json.dumps(data, ensure_ascii=False)
                except Exception:
                    return body_text
        except Exception as e:
            return f"Error: extension request failed: {e}"

    async def _wait_job(
        self,
        job_id: str,
        headers: dict[str, str],
        timeout_seconds: int,
        poll_seconds: int,
    ) -> str:
        timeout_seconds = max(5, int(timeout_seconds))
        poll_seconds = max(1, int(poll_seconds))
        deadline = time.monotonic() + timeout_seconds

        while time.monotonic() < deadline:
            raw = await self._request("GET", f"/jobs/{job_id}", headers=headers)
            parsed = self._safe_loads(raw)
            if isinstance(parsed, dict):
                status = str(parsed.get("status", "")).lower()
                if status in {"done", "completed", "success", "ok"}:
                    result = await self._request("GET", f"/jobs/{job_id}/result", headers=headers)
                    return json.dumps(
                        {"job_id": job_id, "status": status, "result": self._safe_loads(result) or result},
                        ensure_ascii=False,
                    )
                if status in {"error", "failed", "cancelled", "canceled"}:
                    return json.dumps({"job_id": job_id, "status": status, "detail": parsed}, ensure_ascii=False)
            await asyncio.sleep(poll_seconds)

        return json.dumps({"job_id": job_id, "status": "timeout", "timeout_seconds": timeout_seconds}, ensure_ascii=False)

    @staticmethod
    def _safe_loads(raw: str) -> Any | None:
        try:
            return json.loads(raw)
        except Exception:
            return None
