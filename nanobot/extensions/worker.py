"""Lightweight extension worker service for long-running Google jobs."""

from __future__ import annotations

import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from loguru import logger


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _env_bool(name: str, default: bool = False) -> bool:
    value = str(os.environ.get(name, "")).strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


def _require_str(payload: dict[str, Any], key: str) -> str:
    value = str(payload.get(key, "")).strip()
    if not value:
        raise ValueError(f"Missing required field: {key}")
    return value


@dataclass
class Job:
    job_id: str
    task_type: str
    payload: dict[str, Any]
    status: str = "queued"
    created_at: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    result: Any | None = None
    error: str | None = None
    cancel_requested: bool = False

    def to_public(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "task_type": self.task_type,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
        }


class ExtensionJobService:
    """In-memory job queue with worker threads and task handlers."""

    def __init__(self, worker_count: int = 2):
        self.worker_count = max(1, int(worker_count))
        self._lock = threading.RLock()
        self._jobs: dict[str, Job] = {}
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._stop = threading.Event()
        self._workers: list[threading.Thread] = []
        self._web_lock = threading.RLock()
        self._playwright = None
        self._web_context = None
        self._web_page = None
        self._google_profile_dir = str(
            Path(
                os.environ.get(
                    "NANOBOT_GOOGLE_WEB_PROFILE_DIR",
                    "~/.nanobot/playwright/google",
                )
            ).expanduser()
        )
        self._google_headless = _env_bool("NANOBOT_GOOGLE_WEB_HEADLESS", False)
        self._google_browser_channel = (
            str(os.environ.get("NANOBOT_GOOGLE_WEB_BROWSER_CHANNEL", "chrome")).strip() or "chrome"
        )
        self._google_executable_path = str(os.environ.get("NANOBOT_GOOGLE_WEB_EXECUTABLE_PATH", "")).strip() or None
        self._handlers: dict[str, Callable[[dict[str, Any]], Any]] = {
            "google_docs_create": self._google_docs_create,
            "google_docs_update": self._google_docs_update,
            "google_sheet_append": self._google_sheet_append,
            "google_sheet_update": self._google_sheet_update,
            "google_drive_upload": self._google_drive_upload,
            "google_drive_move": self._google_drive_move,
            "google_docs_create_cli": self._google_docs_create_cli,
            "google_docs_update_cli": self._google_docs_update_cli,
            "google_sheet_append_cli": self._google_sheet_append_cli,
            "google_sheet_update_cli": self._google_sheet_update_cli,
            "google_drive_upload_cli": self._google_drive_upload_cli,
            "google_drive_move_cli": self._google_drive_move_cli,
            "google_docs_create_web": self._google_docs_create_web,
            "google_docs_update_web": self._google_docs_update_web,
            "google_docs_open_web": self._google_docs_open_web,
            "google_sheets_open_web": self._google_sheets_open_web,
            "google_drive_open_web": self._google_drive_open_web,
            "test_sleep": self._test_sleep,
        }

    def start(self) -> None:
        for idx in range(self.worker_count):
            thread = threading.Thread(
                target=self._worker_loop,
                name=f"extension-worker-{idx + 1}",
                daemon=True,
            )
            thread.start()
            self._workers.append(thread)
        logger.info("Extension worker started with {} thread(s)", self.worker_count)

    def stop(self) -> None:
        self._stop.set()
        for _ in self._workers:
            self._queue.put(None)
        for thread in self._workers:
            thread.join(timeout=2.0)
        self._workers.clear()
        self._close_web()
        logger.info("Extension worker stopped")

    def submit(self, task_type: str, payload: dict[str, Any]) -> Job:
        task = str(task_type or "").strip()
        if not task:
            raise ValueError("task_type is required")
        job_id = uuid.uuid4().hex
        job = Job(
            job_id=job_id,
            task_type=task,
            payload=payload or {},
            status="queued",
            created_at=_utc_now(),
        )
        with self._lock:
            self._jobs[job_id] = job
        self._queue.put(job_id)
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> Job | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            if job.status in {"done", "failed", "cancelled"}:
                return job
            job.cancel_requested = True
            if job.status == "queued":
                job.status = "cancelled"
                job.finished_at = _utc_now()
            return job

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            job_id = self._queue.get()
            if job_id is None:
                return
            with self._lock:
                job = self._jobs.get(job_id)
                if not job:
                    continue
                if job.status != "queued":
                    continue
                if job.cancel_requested:
                    job.status = "cancelled"
                    job.finished_at = _utc_now()
                    continue
                job.status = "running"
                job.started_at = _utc_now()

            handler = self._handlers.get(job.task_type)
            if not handler:
                with self._lock:
                    job.status = "failed"
                    job.error = f"Unknown task_type: {job.task_type}"
                    job.finished_at = _utc_now()
                continue

            try:
                result = handler(job.payload)
                with self._lock:
                    if job.cancel_requested:
                        job.status = "cancelled"
                    else:
                        job.status = "done"
                        job.result = result
                    job.finished_at = _utc_now()
            except Exception as e:
                with self._lock:
                    job.status = "failed"
                    job.error = str(e)
                    job.finished_at = _utc_now()
                logger.exception("Extension job failed: {} {}", job.job_id, job.task_type)

    @staticmethod
    def _google_clients(scopes: list[str], credentials_file: str | None = None):
        path = credentials_file or os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "")
        if not path:
            raise RuntimeError(
                "Missing Google credentials. Set GOOGLE_SERVICE_ACCOUNT_FILE or payload.credentials_file."
            )
        cred_path = Path(path).expanduser()
        if not cred_path.exists():
            raise RuntimeError(f"Google credentials file not found: {cred_path}")
        try:
            from google.oauth2 import service_account
            from googleapiclient.discovery import build
        except Exception as e:
            raise RuntimeError(
                "Missing Google dependencies. Install: pip install google-api-python-client google-auth"
            ) from e

        creds = service_account.Credentials.from_service_account_file(str(cred_path), scopes=scopes)
        return build("docs", "v1", credentials=creds, cache_discovery=False), build(
            "drive",
            "v3",
            credentials=creds,
            cache_discovery=False,
        ), build("sheets", "v4", credentials=creds, cache_discovery=False)

    @staticmethod
    def _doc_end_index(document: dict[str, Any]) -> int:
        body = (document.get("body") or {}).get("content") or []
        end_index = 1
        for node in body:
            try:
                end_index = max(end_index, int(node.get("endIndex", 1)))
            except Exception:
                continue
        return end_index

    def _google_docs_create(self, payload: dict[str, Any]) -> dict[str, Any]:
        title = str(payload.get("title", "")).strip() or f"nanobot-report-{int(time.time())}"
        content = str(payload.get("content", ""))
        folder_id = str(
            payload.get("folder_id") or os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "")
        ).strip()
        share_anyone_reader = bool(payload.get("share_anyone_reader", False))
        docs, drive, _ = self._google_clients(
            scopes=[
                "https://www.googleapis.com/auth/documents",
                "https://www.googleapis.com/auth/drive",
            ],
            credentials_file=str(payload.get("credentials_file", "")).strip() or None,
        )
        created = docs.documents().create(body={"title": title}).execute()
        doc_id = created.get("documentId")
        if not doc_id:
            raise RuntimeError("Google Docs create did not return documentId")
        if content:
            docs.documents().batchUpdate(
                documentId=doc_id,
                body={"requests": [{"insertText": {"location": {"index": 1}, "text": content}}]},
            ).execute()

        if folder_id:
            current = drive.files().get(fileId=doc_id, fields="parents").execute()
            prev_parents = ",".join(current.get("parents", []))
            drive.files().update(
                fileId=doc_id,
                addParents=folder_id,
                removeParents=prev_parents,
                fields="id, parents",
            ).execute()

        if share_anyone_reader:
            drive.permissions().create(
                fileId=doc_id,
                body={"type": "anyone", "role": "reader"},
            ).execute()

        return {
            "doc_id": doc_id,
            "title": title,
            "url": f"https://docs.google.com/document/d/{doc_id}/edit",
        }

    def _google_docs_update(self, payload: dict[str, Any]) -> dict[str, Any]:
        doc_id = _require_str(payload, "document_id")
        content = str(payload.get("content", ""))
        mode = str(payload.get("mode", "append")).strip().lower()
        docs, _, _ = self._google_clients(
            scopes=["https://www.googleapis.com/auth/documents"],
            credentials_file=str(payload.get("credentials_file", "")).strip() or None,
        )
        doc = docs.documents().get(documentId=doc_id).execute()
        end_index = self._doc_end_index(doc)
        requests: list[dict[str, Any]] = []
        if mode == "replace":
            if end_index > 2:
                requests.append(
                    {
                        "deleteContentRange": {
                            "range": {"startIndex": 1, "endIndex": end_index - 1},
                        }
                    }
                )
            if content:
                requests.append({"insertText": {"location": {"index": 1}, "text": content}})
        else:
            if content:
                insert_index = max(1, end_index - 1)
                requests.append(
                    {"insertText": {"location": {"index": insert_index}, "text": f"\n{content}"}}
                )
        if requests:
            docs.documents().batchUpdate(documentId=doc_id, body={"requests": requests}).execute()
        return {
            "doc_id": doc_id,
            "mode": mode,
            "url": f"https://docs.google.com/document/d/{doc_id}/edit",
        }

    def _google_sheet_append(self, payload: dict[str, Any]) -> dict[str, Any]:
        spreadsheet_id = _require_str(payload, "spreadsheet_id")
        range_name = str(payload.get("range", "Sheet1!A:Z")).strip() or "Sheet1!A:Z"
        values = payload.get("values")
        if not isinstance(values, list) or not values:
            raise ValueError("values must be a non-empty list")
        if values and not isinstance(values[0], list):
            values = [values]
        value_input_option = str(payload.get("value_input_option", "USER_ENTERED")).strip()
        _, _, sheets = self._google_clients(
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
            credentials_file=str(payload.get("credentials_file", "")).strip() or None,
        )
        resp = sheets.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range=range_name,
            valueInputOption=value_input_option,
            insertDataOption="INSERT_ROWS",
            body={"values": values},
        ).execute()
        return {
            "spreadsheet_id": spreadsheet_id,
            "range": range_name,
            "updates": resp.get("updates", {}),
            "url": f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit",
        }

    def _google_sheet_update(self, payload: dict[str, Any]) -> dict[str, Any]:
        spreadsheet_id = _require_str(payload, "spreadsheet_id")
        range_name = _require_str(payload, "range")
        values = payload.get("values")
        if not isinstance(values, list) or not values:
            raise ValueError("values must be a non-empty list")
        if values and not isinstance(values[0], list):
            values = [values]
        value_input_option = str(payload.get("value_input_option", "USER_ENTERED")).strip()
        _, _, sheets = self._google_clients(
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
            credentials_file=str(payload.get("credentials_file", "")).strip() or None,
        )
        resp = sheets.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=range_name,
            valueInputOption=value_input_option,
            body={"values": values},
        ).execute()
        return {
            "spreadsheet_id": spreadsheet_id,
            "range": range_name,
            "updated_cells": resp.get("updatedCells", 0),
            "url": f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit",
        }

    def _google_drive_upload(self, payload: dict[str, Any]) -> dict[str, Any]:
        local_path = Path(_require_str(payload, "local_path")).expanduser()
        if not local_path.exists() or not local_path.is_file():
            raise FileNotFoundError(f"local_path not found: {local_path}")
        file_name = str(payload.get("file_name", "")).strip() or local_path.name
        folder_id = str(
            payload.get("folder_id") or os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "")
        ).strip()
        mime_type = str(payload.get("mime_type", "")).strip() or None
        share_anyone_reader = bool(payload.get("share_anyone_reader", False))
        _, drive, _ = self._google_clients(
            scopes=["https://www.googleapis.com/auth/drive"],
            credentials_file=str(payload.get("credentials_file", "")).strip() or None,
        )
        try:
            from googleapiclient.http import MediaFileUpload
        except Exception as e:
            raise RuntimeError(
                "Missing Google dependencies. Install: pip install google-api-python-client google-auth"
            ) from e

        metadata: dict[str, Any] = {"name": file_name}
        if folder_id:
            metadata["parents"] = [folder_id]
        media = MediaFileUpload(str(local_path), mimetype=mime_type, resumable=False)
        created = drive.files().create(
            body=metadata,
            media_body=media,
            fields="id,name,webViewLink,webContentLink,parents",
        ).execute()
        file_id = created.get("id")
        if not file_id:
            raise RuntimeError("Google Drive upload did not return id")
        if share_anyone_reader:
            drive.permissions().create(
                fileId=file_id,
                body={"type": "anyone", "role": "reader"},
            ).execute()
        url = created.get("webViewLink") or f"https://drive.google.com/file/d/{file_id}/view"
        return {
            "file_id": file_id,
            "name": created.get("name", file_name),
            "url": url,
            "parents": created.get("parents", []),
        }

    def _google_drive_move(self, payload: dict[str, Any]) -> dict[str, Any]:
        file_id = _require_str(payload, "file_id")
        folder_id = _require_str(payload, "folder_id")
        _, drive, _ = self._google_clients(
            scopes=["https://www.googleapis.com/auth/drive"],
            credentials_file=str(payload.get("credentials_file", "")).strip() or None,
        )
        current = drive.files().get(fileId=file_id, fields="parents").execute()
        prev_parents = ",".join(current.get("parents", []))
        updated = drive.files().update(
            fileId=file_id,
            addParents=folder_id,
            removeParents=prev_parents,
            fields="id,parents,webViewLink",
        ).execute()
        return {
            "file_id": updated.get("id", file_id),
            "parents": updated.get("parents", []),
            "url": updated.get("webViewLink", f"https://drive.google.com/file/d/{file_id}/view"),
        }

    def _run_workspace_cli(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        python_bin = str(os.environ.get("NANOBOT_GOOGLE_CLI_PYTHON", sys.executable)).strip() or sys.executable
        timeout_s = max(5, int(os.environ.get("NANOBOT_GOOGLE_CLI_TIMEOUT_SECONDS", "120")))
        cmd = [
            python_bin,
            "-m",
            "nanobot.extensions.google_workspace_cli",
            "run",
            "--action",
            action,
            "--payload-json",
            json.dumps(payload or {}, ensure_ascii=False),
        ]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(f"workspace-cli failed ({action}): {detail[:800]}")
        output = (proc.stdout or "").strip()
        try:
            parsed = json.loads(output)
            if isinstance(parsed, dict):
                return parsed
            return {"raw": output}
        except Exception:
            return {"raw": output}

    def _google_docs_create_cli(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._run_workspace_cli("docs_create", payload)

    def _google_docs_update_cli(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._run_workspace_cli("docs_update", payload)

    def _google_sheet_append_cli(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._run_workspace_cli("sheets_append", payload)

    def _google_sheet_update_cli(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._run_workspace_cli("sheets_update", payload)

    def _google_drive_upload_cli(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._run_workspace_cli("drive_upload", payload)

    def _google_drive_move_cli(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._run_workspace_cli("drive_move", payload)

    @staticmethod
    def _test_sleep(payload: dict[str, Any]) -> dict[str, Any]:
        seconds = max(0, int(payload.get("seconds", 3)))
        time.sleep(seconds)
        return {"slept_seconds": seconds}

    def _close_web(self) -> None:
        with self._web_lock:
            if self._web_context is not None:
                try:
                    self._web_context.close()
                except Exception:
                    pass
                self._web_context = None
                self._web_page = None
            if self._playwright is not None:
                try:
                    self._playwright.stop()
                except Exception:
                    pass
                self._playwright = None

    def _ensure_web_page(self):
        with self._web_lock:
            if self._web_page is not None:
                try:
                    if not self._web_page.is_closed():
                        return self._web_page
                except Exception:
                    pass

            try:
                from playwright.sync_api import sync_playwright
            except Exception as e:
                raise RuntimeError(
                    "Playwright is required for web-mode Google tasks. "
                    "Install with: pip install playwright && playwright install chromium"
                ) from e

            Path(self._google_profile_dir).mkdir(parents=True, exist_ok=True)
            if self._playwright is None:
                self._playwright = sync_playwright().start()

            launch_opts: dict[str, Any] = {
                "user_data_dir": self._google_profile_dir,
                "headless": self._google_headless,
                "viewport": {"width": 1440, "height": 960},
                "ignore_default_args": ["--enable-automation"],
                "args": [
                    "--disable-blink-features=AutomationControlled",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
            }
            if self._google_browser_channel:
                launch_opts["channel"] = self._google_browser_channel
            if self._google_executable_path:
                launch_opts["executable_path"] = self._google_executable_path

            self._web_context = self._playwright.chromium.launch_persistent_context(**launch_opts)
            self._web_page = (
                self._web_context.pages[0] if self._web_context.pages else self._web_context.new_page()
            )
            return self._web_page

    def _web_open(self, url: str, timeout_ms: int = 90000) -> str:
        page = self._ensure_web_page()
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        self._ensure_google_login(page)
        return page.url

    def _ensure_google_login(self, page: Any) -> None:
        current = str(page.url or "")
        if "accounts.google.com" in current:
            raise RuntimeError(
                "Google login required for web mode. "
                f"Please login once in browser profile: {self._google_profile_dir}"
            )

    @staticmethod
    def _extract_doc_id(url: str) -> str | None:
        m = re.search(r"/document/d/([a-zA-Z0-9_-]+)", url)
        return m.group(1) if m else None

    @staticmethod
    def _extract_sheet_id(url: str) -> str | None:
        m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", url)
        return m.group(1) if m else None

    @staticmethod
    def _wait_visible(page: Any, selectors: list[str], timeout_s: float = 30.0) -> Any | None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            for selector in selectors:
                loc = page.locator(selector).first
                try:
                    if loc.count() > 0 and loc.is_visible():
                        return loc
                except Exception:
                    continue
            time.sleep(0.2)
        return None

    def _set_doc_title(self, page: Any, title: str) -> None:
        if not title:
            return
        selectors = [
            'input[aria-label="Rename"]',
            "#docs-title-input",
            "input.docs-title-input",
        ]
        loc = self._wait_visible(page, selectors, timeout_s=10.0)
        if loc is None:
            return
        try:
            loc.click(timeout=1000)
            loc.fill(title, timeout=1000)
        except Exception:
            try:
                loc.press("Control+A")
                loc.press("Backspace")
                loc.type(title, delay=0)
            except Exception:
                return

    def _focus_doc_editor(self, page: Any) -> None:
        editor_selectors = [
            "div.kix-appview-editor",
            "div#docs-editor",
            "iframe.docs-texteventtarget-iframe",
        ]
        loc = self._wait_visible(page, editor_selectors, timeout_s=25.0)
        if loc is None:
            raise RuntimeError("Cannot find Google Docs editor area")
        try:
            loc.click(timeout=1500)
        except Exception:
            try:
                page.click("body", timeout=1000)
            except Exception as e:
                raise RuntimeError("Cannot focus Google Docs editor") from e

    def _type_doc_content(self, page: Any, content: str, mode: str = "append") -> None:
        if not content:
            return
        self._focus_doc_editor(page)
        normalized_mode = str(mode or "append").strip().lower()
        if normalized_mode == "replace":
            page.keyboard.press("Control+A")
            page.keyboard.press("Backspace")
            page.keyboard.insert_text(content)
            return
        page.keyboard.insert_text(content if content.startswith("\n") else f"\n{content}")

    def _google_docs_create_web(self, payload: dict[str, Any]) -> dict[str, Any]:
        title = str(payload.get("title", "")).strip() or f"nanobot-web-doc-{int(time.time())}"
        content = str(payload.get("content", ""))
        with self._web_lock:
            final_url = self._web_open("https://docs.new/")
            page = self._ensure_web_page()
            self._set_doc_title(page, title)
            if content:
                self._type_doc_content(page, content, mode="replace")
            settle = max(0.0, min(float(payload.get("settle_seconds", 1.5)), 10.0))
            if settle > 0:
                time.sleep(settle)
            final_url = page.url or final_url
        return {
            "mode": "web",
            "doc_id": self._extract_doc_id(final_url),
            "title": title,
            "url": final_url,
            "content_chars": len(content),
            "profile_dir": self._google_profile_dir,
        }

    def _google_docs_update_web(self, payload: dict[str, Any]) -> dict[str, Any]:
        doc_url = str(payload.get("url", "")).strip()
        doc_id = str(payload.get("document_id", "")).strip()
        if not doc_url:
            if not doc_id:
                raise ValueError("Missing url or document_id")
            doc_url = f"https://docs.google.com/document/d/{doc_id}/edit"
        content = str(payload.get("content", ""))
        mode = str(payload.get("mode", "append")).strip().lower() or "append"
        with self._web_lock:
            final_url = self._web_open(doc_url)
            page = self._ensure_web_page()
            if content:
                self._type_doc_content(page, content, mode=mode)
            settle = max(0.0, min(float(payload.get("settle_seconds", 1.0)), 10.0))
            if settle > 0:
                time.sleep(settle)
            final_url = page.url or final_url
        return {
            "mode": "web",
            "doc_id": self._extract_doc_id(final_url),
            "url": final_url,
            "updated_chars": len(content),
            "update_mode": mode,
            "profile_dir": self._google_profile_dir,
        }

    def _google_docs_open_web(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = str(payload.get("url", "")).strip()
        doc_id = str(payload.get("document_id", "")).strip()
        if not url:
            url = (
                f"https://docs.google.com/document/d/{doc_id}/edit"
                if doc_id
                else "https://docs.google.com/document/u/0/"
            )
        with self._web_lock:
            final_url = self._web_open(url)
        return {
            "mode": "web",
            "doc_id": self._extract_doc_id(final_url),
            "url": final_url,
            "profile_dir": self._google_profile_dir,
        }

    def _google_sheets_open_web(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = str(payload.get("url", "")).strip()
        sheet_id = str(payload.get("spreadsheet_id", "")).strip()
        if not url:
            url = (
                f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"
                if sheet_id
                else "https://docs.google.com/spreadsheets/u/0/"
            )
        with self._web_lock:
            final_url = self._web_open(url)
        return {
            "mode": "web",
            "spreadsheet_id": self._extract_sheet_id(final_url),
            "url": final_url,
            "profile_dir": self._google_profile_dir,
        }

    def _google_drive_open_web(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = str(payload.get("url", "")).strip()
        folder_id = str(payload.get("folder_id", "")).strip()
        if not url:
            url = (
                f"https://drive.google.com/drive/folders/{folder_id}"
                if folder_id
                else "https://drive.google.com/drive/u/0/my-drive"
            )
        with self._web_lock:
            final_url = self._web_open(url)
        return {
            "mode": "web",
            "folder_id": folder_id or None,
            "url": final_url,
            "profile_dir": self._google_profile_dir,
        }


def create_http_handler(service: ExtensionJobService, token: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = "nanobot-extension-worker/0.1"

        def _json(self, code: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _auth_ok(self) -> bool:
            if not token:
                return True
            auth = self.headers.get("Authorization", "").strip()
            expected = f"Bearer {token}"
            return auth == expected

        def _read_json_body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            if not raw:
                return {}
            parsed = json.loads(raw.decode("utf-8"))
            if not isinstance(parsed, dict):
                raise ValueError("JSON body must be an object")
            return parsed

        def do_GET(self) -> None:
            if not self._auth_ok():
                self._json(401, {"error": "Unauthorized"})
                return

            parsed = urlparse(self.path)
            parts = [p for p in parsed.path.split("/") if p]
            if parsed.path == "/health":
                self._json(200, {"ok": True})
                return

            if len(parts) == 2 and parts[0] == "jobs":
                job = service.get(parts[1])
                if not job:
                    self._json(404, {"error": "Job not found"})
                    return
                self._json(200, job.to_public())
                return

            if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "result":
                job = service.get(parts[1])
                if not job:
                    self._json(404, {"error": "Job not found"})
                    return
                if job.status != "done":
                    self._json(
                        409,
                        {"error": f"Job is not done: {job.status}", "job": job.to_public()},
                    )
                    return
                self._json(200, {"job_id": job.job_id, "status": job.status, "result": job.result})
                return

            self._json(404, {"error": "Not found"})

        def do_POST(self) -> None:
            if not self._auth_ok():
                self._json(401, {"error": "Unauthorized"})
                return

            parsed = urlparse(self.path)
            parts = [p for p in parsed.path.split("/") if p]

            if parsed.path == "/jobs":
                try:
                    payload = self._read_json_body()
                    task_type = str(payload.get("task_type", "")).strip()
                    job_payload = payload.get("payload")
                    if job_payload is None:
                        job_payload = {}
                    if not isinstance(job_payload, dict):
                        raise ValueError("payload must be an object")
                    job = service.submit(task_type=task_type, payload=job_payload)
                    self._json(200, job.to_public())
                    return
                except Exception as e:
                    self._json(400, {"error": str(e)})
                    return

            if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "cancel":
                job = service.cancel(parts[1])
                if not job:
                    self._json(404, {"error": "Job not found"})
                    return
                self._json(200, job.to_public())
                return

            self._json(404, {"error": "Not found"})

        def log_message(self, fmt: str, *args: Any) -> None:
            logger.debug("Extension worker HTTP: " + fmt, *args)

    return Handler


def run_extension_worker_server(
    host: str = "127.0.0.1",
    port: int = 7091,
    token: str | None = None,
    worker_count: int = 2,
) -> None:
    resolved_token = (token or os.environ.get("NANOBOT_EXTENSION_TOKEN", "")).strip()
    service = ExtensionJobService(worker_count=worker_count)
    handler = create_http_handler(service, resolved_token)
    server = ThreadingHTTPServer((host, int(port)), handler)
    service.start()
    logger.info(
        "Extension worker listening at http://{}:{} (auth={})",
        host,
        port,
        "on" if resolved_token else "off",
    )

    stop_event = threading.Event()

    def _graceful_stop(signum: int, _frame: Any) -> None:
        logger.info("Extension worker received signal {}, shutting down...", signum)
        stop_event.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    can_install_signals = threading.current_thread() is threading.main_thread()
    previous_int = signal.getsignal(signal.SIGINT) if can_install_signals else None
    previous_term = signal.getsignal(signal.SIGTERM) if can_install_signals else None
    if can_install_signals:
        signal.signal(signal.SIGINT, _graceful_stop)
        signal.signal(signal.SIGTERM, _graceful_stop)

    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        service.stop()
        server.server_close()
        if can_install_signals and previous_int is not None and previous_term is not None:
            signal.signal(signal.SIGINT, previous_int)
            signal.signal(signal.SIGTERM, previous_term)
        if stop_event.is_set():
            logger.info("Extension worker stopped gracefully")
