#!/usr/bin/env python3
"""Publish a local text/markdown report to Google Docs and print share link."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _read_content(input_file: str | None) -> str:
    if input_file:
        path = Path(input_file).expanduser()
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(f"Input file not found: {path}")
        return path.read_text(encoding="utf-8")
    data = sys.stdin.read()
    if not data.strip():
        raise ValueError("No input content. Provide --input-file or pipe content via stdin.")
    return data


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Publish report to Google Docs and print URL.")
    p.add_argument("--title", required=True, help="Document title")
    p.add_argument("--input-file", help="Path to markdown/text file")
    p.add_argument(
        "--credentials",
        default=os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", ""),
        help="Path to service account JSON (default: GOOGLE_SERVICE_ACCOUNT_FILE)",
    )
    p.add_argument(
        "--folder-id",
        default=os.environ.get("GOOGLE_DRIVE_FOLDER_ID", ""),
        help="Drive folder ID to place document into",
    )
    p.add_argument(
        "--share-anyone-reader",
        action="store_true",
        help="Grant read access to anyone with link",
    )
    return p


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    if not args.credentials:
        print(
            "ERROR: Missing credentials. Set GOOGLE_SERVICE_ACCOUNT_FILE or pass --credentials.",
            file=sys.stderr,
        )
        return 2

    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except Exception:
        print(
            "ERROR: Missing Google API dependencies. Install with: "
            "pip install google-api-python-client google-auth",
            file=sys.stderr,
        )
        return 2

    try:
        content = _read_content(args.input_file)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    scopes = [
        "https://www.googleapis.com/auth/documents",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = service_account.Credentials.from_service_account_file(args.credentials, scopes=scopes)

    docs = build("docs", "v1", credentials=creds)
    drive = build("drive", "v3", credentials=creds)

    try:
        created = docs.documents().create(body={"title": args.title}).execute()
        doc_id = created.get("documentId")
        if not doc_id:
            print("ERROR: Failed to create Google Doc.", file=sys.stderr)
            return 1

        docs.documents().batchUpdate(
            documentId=doc_id,
            body={"requests": [{"insertText": {"location": {"index": 1}, "text": content}}]},
        ).execute()

        if args.folder_id:
            current = drive.files().get(fileId=doc_id, fields="parents").execute()
            prev_parents = ",".join(current.get("parents", []))
            drive.files().update(
                fileId=doc_id,
                addParents=args.folder_id,
                removeParents=prev_parents,
                fields="id, parents",
            ).execute()

        if args.share_anyone_reader:
            drive.permissions().create(
                fileId=doc_id,
                body={"type": "anyone", "role": "reader"},
            ).execute()

        url = f"https://docs.google.com/document/d/{doc_id}/edit"
        print(url)
        return 0
    except Exception as e:
        print(f"ERROR: Failed to publish Google Doc: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

