---
name: schedule-manager
description: Full schedule manager for reminders (add/update/delete/list/enable/disable) with hourly and weekday patterns.
metadata: {"nanobot":{"emoji":"⏰"}}
always: true
---

# Schedule Manager

Use this skill when user asks:
- set reminder
- recurring schedule
- daily/weekly plans
- morning check-in automation
- edit existing reminders
- delete reminders
- list schedule by day/week/month

When triggered, manage jobs directly via `cron` tool.
Do not ask user to run shell commands manually.

## Supported Operations (CRUD)

1. `add`: create new schedule
2. `update`/`edit`: modify message/time/frequency
3. `remove`/`delete`: delete job
4. `enable`/`disable`: turn jobs on/off
5. `list`: show schedule (`period=all|day|week|month`)

## Schedule Patterns

### 1) One-time reminder (at specific datetime)

```python
cron(
  action="add",
  name="nhac-phoi-do",
  message="Nhắc anh đi phơi đồ",
  at="2026-03-03T10:20:00"
)
```

### 2) Repeat every hour

```python
cron(
  action="add",
  name="review-hourly",
  message="Nhắc review nhanh công việc hiện tại",
  every_hours=1
)
```

### 3) Daily fixed time

```python
cron(
  action="add",
  name="morning-plan",
  message="Nhắc check kế hoạch buổi sáng",
  hour=5,
  minute=0
)
```

### 4) Weekly (specific weekdays)

```python
cron(
  action="add",
  name="hoc-t2-t4-t6",
  message="Nhắc block học tập",
  hour=20,
  minute=0,
  weekdays="mon,wed,fri"
)
```

Alternative weekday format:
- `"1-5"` (Mon..Fri)
- `"thu2,thu4,thu6"`
- `"cn"` (Sunday)

## Update / Edit

```python
cron(
  action="update",
  job_id="<job_id>",
  message="Nhắc mới đã chỉnh nội dung",
  hour=21,
  minute=30,
  weekdays="2,4,6"
)
```

Only update message:
```python
cron(action="update", job_id="<job_id>", message="Nội dung mới")
```

Enable/disable:
```python
cron(action="disable", job_id="<job_id>")
cron(action="enable", job_id="<job_id>")
```

Delete:
```python
cron(action="remove", job_id="<job_id>")
# or: cron(action="delete", job_id="<job_id>")
```

## List by Day / Week / Month

All jobs:
```python
cron(action="list", period="all")
```

Today:
```python
cron(action="list", period="day")
```

This week:
```python
cron(action="list", period="week")
```

This month:
```python
cron(action="list", period="month")
```

Specific date anchor:
```python
cron(action="list", period="week", date="2026-03-03")
```

## Behavior Rules

- If user asks to "sửa lịch", prefer `action="update"` instead of remove + add.
- If user asks "xóa lịch", remove only the targeted job ID.
- After add/update/delete, always return:
  - job id
  - effective schedule
  - next run
- Default timezone must follow RTC timezone via `NANOBOT_RTC_TIMEZONE` (fallback `UTC`) unless user explicitly provides `tz`.
- If there are multiple matching jobs and user request is ambiguous, ask which job id to edit/delete.
