---
name: daily-ops
description: Build and run recurring daily workflows with supervisor loops, reporting, and background branching.
metadata: {"nanobot":{"emoji":"🧭"}}
---

# Daily Ops

Use this skill for:
- daily work/study routines
- recurring checklists
- monitoring long jobs until done
- sending periodic status + final report

## Core Pattern

1. Create recurring schedule via `cron`.
2. Delegate heavy actions to `extension_job`.
3. Poll job status (`extension_job` action=`wait` or `status` loop).
4. Send concise progress updates.
5. Send final report with links/artifacts.

## Branching Rule

If user sends a new request while another job is running:
- handle as a separate branch/session
- do not block the new request
- keep both streams reportable

## Recommended Daily Job Set

1. Morning reminder
2. Learning block tracker
3. Work review summary
4. End-of-day report to Docs + Sheet row log

## Example Cron Messages

- `"Daily planning: collect tasks, create Docs report, append KPI row to Sheet, send link."`
- `"Study loop: summarize notes into Docs, update progress Sheet, report top gaps."`

