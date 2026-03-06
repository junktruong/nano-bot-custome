---
name: google-workspace
description: Operate Google Docs, Sheets, and Drive through extension workers and return links/results.
metadata: {"nanobot":{"emoji":"📊"}}
---

# Google Workspace

Use this skill when tasks involve:
- Google Docs content generation
- Google Sheets updates/logging
- Google Drive storage and file organization

## Execution Strategy

Use `extension_job` tool as worker gateway.

Expected worker task types (example contract):
- CLI mode (preferred when OAuth already connected):
- `google_docs_create_cli`
- `google_docs_update_cli`
- `google_sheet_append_cli`
- `google_sheet_update_cli`
- `google_drive_upload_cli`
- `google_drive_move_cli`
- Web mode (no Google API credentials, reuse logged-in browser profile):
- `google_docs_create_web`
- `google_docs_update_web`
- `google_docs_open_web`
- `google_sheets_open_web`
- `google_drive_open_web`
- API mode (when service account is available):
- `google_docs_create`
- `google_docs_update`
- `google_sheet_append`
- `google_sheet_update`
- `google_drive_upload`
- `google_drive_move`

## Standard Flow

1. Submit worker job:
- `extension_job(action="submit", task_type="google_docs_create_cli", payload={...})`
2. Wait until completion:
- `extension_job(action="wait", job_id="...")`
3. Extract URL/id from result.
4. Send user final summary + link(s).

## Reporting Format

- Objective
- What was updated (Docs/Sheets/Drive)
- Links
- Errors/retries if any

## Reliability

- Retry transient worker failures.
- If worker times out, report partial output + next step.
- Never silently swallow failed Google actions.
- Prefer CLI mode over web mode when OAuth is available.
