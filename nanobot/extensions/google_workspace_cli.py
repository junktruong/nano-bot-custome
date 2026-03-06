"""Google Workspace CLI for Docs/Sheets/Drive with user OAuth."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]


def _client_file() -> Path:
    raw = os.environ.get("NANOBOT_GOOGLE_OAUTH_CLIENT_FILE", "~/.nanobot/google/credentials.json")
    return Path(raw).expanduser()


def _token_file() -> Path:
    raw = os.environ.get("NANOBOT_GOOGLE_OAUTH_TOKEN_FILE", "~/.nanobot/google/token.json")
    return Path(raw).expanduser()


def _google_build_services(creds):
    from googleapiclient.discovery import build

    docs = build("docs", "v1", credentials=creds, cache_discovery=False)
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
    return docs, drive, sheets


def _load_credentials(require_valid: bool = True):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    token_path = _token_file()
    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json(), encoding="utf-8")

    if require_valid and (not creds or not creds.valid):
        raise RuntimeError(
            "Google OAuth chưa sẵn sàng. Chạy: "
            "python -m nanobot.extensions.google_workspace_cli auth login --no-local-server"
        )
    return creds


def _auth_login(no_local_server: bool = False) -> dict[str, Any]:
    from google_auth_oauthlib.flow import InstalledAppFlow

    client_path = _client_file()
    if not client_path.exists():
        raise RuntimeError(
            f"Không tìm thấy OAuth client file: {client_path}. "
            "Tải Desktop OAuth credentials từ Google Cloud Console."
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(client_path), SCOPES)
    if no_local_server:
        creds = flow.run_console()
    else:
        creds = flow.run_local_server(port=0)

    token_path = _token_file()
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    return {
        "ok": True,
        "token_file": str(token_path),
        "scopes": SCOPES,
    }


def _auth_status() -> dict[str, Any]:
    token_path = _token_file()
    try:
        creds = _load_credentials(require_valid=False)
    except Exception:
        creds = None
    return {
        "token_file": str(token_path),
        "token_exists": token_path.exists(),
        "valid": bool(creds and creds.valid),
        "expired": bool(creds.expired) if creds else None,
        "has_refresh_token": bool(creds and creds.refresh_token),
    }


def _doc_end_index(document: dict[str, Any]) -> int:
    body = (document.get("body") or {}).get("content") or []
    end_index = 1
    for node in body:
        try:
            end_index = max(end_index, int(node.get("endIndex", 1)))
        except Exception:
            continue
    return end_index


def _act_docs_create(payload: dict[str, Any]) -> dict[str, Any]:
    title = str(payload.get("title", "")).strip() or "nanobot-doc"
    content = str(payload.get("content", ""))
    folder_id = str(payload.get("folder_id", "")).strip()
    share_anyone_reader = bool(payload.get("share_anyone_reader", False))

    creds = _load_credentials(require_valid=True)
    docs, drive, _ = _google_build_services(creds)

    created = docs.documents().create(body={"title": title}).execute()
    doc_id = created.get("documentId")
    if not doc_id:
        raise RuntimeError("docs.create không trả về documentId")

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
            fields="id,parents",
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


def _act_docs_update(payload: dict[str, Any]) -> dict[str, Any]:
    doc_id = str(payload.get("document_id", "")).strip()
    if not doc_id:
        raise ValueError("document_id is required")
    content = str(payload.get("content", ""))
    mode = str(payload.get("mode", "append")).strip().lower()

    creds = _load_credentials(require_valid=True)
    docs, _, _ = _google_build_services(creds)

    doc = docs.documents().get(documentId=doc_id).execute()
    end_index = _doc_end_index(doc)
    requests: list[dict[str, Any]] = []
    if mode == "replace":
        if end_index > 2:
            requests.append(
                {"deleteContentRange": {"range": {"startIndex": 1, "endIndex": end_index - 1}}}
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


def _act_sheets_append(payload: dict[str, Any]) -> dict[str, Any]:
    spreadsheet_id = str(payload.get("spreadsheet_id", "")).strip()
    if not spreadsheet_id:
        raise ValueError("spreadsheet_id is required")
    range_name = str(payload.get("range", "Sheet1!A:Z")).strip() or "Sheet1!A:Z"
    values = payload.get("values")
    if not isinstance(values, list) or not values:
        raise ValueError("values must be a non-empty list")
    if values and not isinstance(values[0], list):
        values = [values]
    value_input_option = str(payload.get("value_input_option", "USER_ENTERED")).strip()

    creds = _load_credentials(require_valid=True)
    _, _, sheets = _google_build_services(creds)
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


def _act_sheets_update(payload: dict[str, Any]) -> dict[str, Any]:
    spreadsheet_id = str(payload.get("spreadsheet_id", "")).strip()
    if not spreadsheet_id:
        raise ValueError("spreadsheet_id is required")
    range_name = str(payload.get("range", "")).strip()
    if not range_name:
        raise ValueError("range is required")
    values = payload.get("values")
    if not isinstance(values, list) or not values:
        raise ValueError("values must be a non-empty list")
    if values and not isinstance(values[0], list):
        values = [values]
    value_input_option = str(payload.get("value_input_option", "USER_ENTERED")).strip()

    creds = _load_credentials(require_valid=True)
    _, _, sheets = _google_build_services(creds)
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


def _act_drive_upload(payload: dict[str, Any]) -> dict[str, Any]:
    local_path = Path(str(payload.get("local_path", "")).strip()).expanduser()
    if not local_path.exists() or not local_path.is_file():
        raise FileNotFoundError(f"local_path not found: {local_path}")
    file_name = str(payload.get("file_name", "")).strip() or local_path.name
    folder_id = str(payload.get("folder_id", "")).strip()
    mime_type = str(payload.get("mime_type", "")).strip() or None
    share_anyone_reader = bool(payload.get("share_anyone_reader", False))

    from googleapiclient.http import MediaFileUpload

    creds = _load_credentials(require_valid=True)
    _, drive, _ = _google_build_services(creds)
    metadata: dict[str, Any] = {"name": file_name}
    if folder_id:
        metadata["parents"] = [folder_id]
    media = MediaFileUpload(str(local_path), mimetype=mime_type, resumable=False)
    created = drive.files().create(
        body=metadata,
        media_body=media,
        fields="id,name,webViewLink,parents",
    ).execute()
    file_id = created.get("id")
    if not file_id:
        raise RuntimeError("drive.upload không trả về file id")

    if share_anyone_reader:
        drive.permissions().create(
            fileId=file_id,
            body={"type": "anyone", "role": "reader"},
        ).execute()
    return {
        "file_id": file_id,
        "name": created.get("name", file_name),
        "url": created.get("webViewLink") or f"https://drive.google.com/file/d/{file_id}/view",
    }


def _act_drive_move(payload: dict[str, Any]) -> dict[str, Any]:
    file_id = str(payload.get("file_id", "")).strip()
    folder_id = str(payload.get("folder_id", "")).strip()
    if not file_id or not folder_id:
        raise ValueError("file_id and folder_id are required")

    creds = _load_credentials(require_valid=True)
    _, drive, _ = _google_build_services(creds)
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


def _run_action(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    mapping = {
        "docs_create": _act_docs_create,
        "docs_update": _act_docs_update,
        "sheets_append": _act_sheets_append,
        "sheets_update": _act_sheets_update,
        "drive_upload": _act_drive_upload,
        "drive_move": _act_drive_move,
    }
    fn = mapping.get(action)
    if not fn:
        raise ValueError(f"Unknown action: {action}")
    return fn(payload)


def _print_json(obj: dict[str, Any]) -> None:
    print(json.dumps(obj, ensure_ascii=False))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="nanobot Google Workspace CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    auth = sub.add_parser("auth", help="OAuth auth operations")
    auth_sub = auth.add_subparsers(dest="auth_command", required=True)
    auth_login = auth_sub.add_parser("login", help="Login and save OAuth token")
    auth_login.add_argument("--no-local-server", action="store_true")
    auth_sub.add_parser("status", help="Check OAuth token status")

    run = sub.add_parser("run", help="Run one Workspace action")
    run.add_argument("--action", required=True)
    run.add_argument("--payload-json", default="{}")
    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    try:
        if args.command == "auth":
            if args.auth_command == "login":
                _print_json(_auth_login(no_local_server=bool(args.no_local_server)))
                return 0
            if args.auth_command == "status":
                _print_json(_auth_status())
                return 0
            raise ValueError(f"Unknown auth command: {args.auth_command}")

        if args.command == "run":
            payload = json.loads(args.payload_json or "{}")
            if not isinstance(payload, dict):
                raise ValueError("payload-json must be a JSON object")
            _print_json(_run_action(args.action, payload))
            return 0

        raise ValueError(f"Unknown command: {args.command}")
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

