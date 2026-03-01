---
name: schedule-manager
description: Manage reminders and recurring plans using cron, including daily 05:00 reminders.
metadata: {"nanobot":{"emoji":"⏰"}}
always: true
---

# Schedule Manager

Use this skill when user asks:
- set reminder
- recurring schedule
- daily/weekly plans
- morning check-in automation

When triggered, create/remove/list jobs directly via `cron` tool.
Do not ask user to run shell commands manually.

## Separate Jobs (Recommended)

Always create **2 independent jobs** so user can enable/disable each flow separately:

1. `daily-reminder` (nhắc lịch)
2. `daily-research-doc` (research + publish Google Docs + trả link)

### Job 1: Daily Reminder 05:00

```python
cron(
  action="add",
  message="Nhắc lịch buổi sáng: kiểm tra kế hoạch trong ngày.",
  cron_expr="0 5 * * *",
  tz="Asia/Ho_Chi_Minh"
)
```

### Job 2: Daily Research 05:10

Run a bit later to avoid overlapping work:

```python
cron(
  action="add",
  message="Research chủ đề đã cấu hình, publish vào Google Docs và gửi link đọc.",
  cron_expr="10 5 * * *",
  tz="Asia/Ho_Chi_Minh"
)
```

## CLI Commands (Explicit Separate Jobs)

`<CHAT_ID>` là Zalo chat id nhận kết quả.

```bash
# Job 1: reminder
nanobot cron add \
  --name "daily-reminder" \
  --message "Nhắc lịch buổi sáng: kiểm tra kế hoạch trong ngày." \
  --cron "0 5 * * *" \
  --tz "Asia/Ho_Chi_Minh" \
  --deliver \
  --channel "zalo" \
  --to "<CHAT_ID>"

# Job 2: research + docs
nanobot cron add \
  --name "daily-research-doc" \
  --message "Research chủ đề thị trường AI hôm nay, publish vào Google Docs và gửi link đọc." \
  --cron "10 5 * * *" \
  --tz "Asia/Ho_Chi_Minh" \
  --deliver \
  --channel "zalo" \
  --to "<CHAT_ID>"
```

## Script Bootstrap

Use helper script:

```bash
bash nanobot/skills/schedule-manager/scripts/create_separate_jobs.sh "<CHAT_ID>"
```

Options:
- `--channel zalo` (default)
- `--reminder-time "0 5 * * *"`
- `--research-time "10 5 * * *"`

## Operations

List jobs:
```python
cron(action="list")
```

Remove job:
```python
cron(action="remove", job_id="<job_id>")
```

Disable/enable specific job:
- use CLI: `nanobot cron enable <job_id> --disable`
- re-enable: `nanobot cron enable <job_id>`

One-time reminder:
```python
cron(action="add", message="Nhắc họp với team", at="2026-03-01T14:00:00")
```
