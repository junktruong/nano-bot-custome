---
name: research
description: Research a topic, produce a structured report, publish it to Google Docs, and return the readable link.
metadata: {"nanobot":{"emoji":"🔎"}}
always: true
---

# Research

Use this skill when the user asks for:
- deep research
- market/competitor analysis
- compare options before decision
- a report with sources and final recommendation

When triggered, execute workflow end-to-end. Do not respond with only "run these commands".

## Required Outcome

After finishing research:
1. Publish the report to Google Docs
2. Return the Google Docs link to the user

If Google Docs publish fails, return:
- concise summary in chat
- the local report file path
- exact error and missing setup

## Workflow

1. Clarify scope quickly:
- topic
- audience
- depth
- deadline

2. Research from multiple sources:
- prioritize official docs, primary data, or reputable sources
- keep source URLs for citation

3. Build report content with this structure:
- Executive Summary
- Key Findings
- Analysis (pros/cons, tradeoffs)
- Recommendation
- Sources

4. Save report markdown to a file, for example:
`/tmp/research_report.md`

5. Publish to Google Docs using script:

```bash
python3 nanobot/skills/research/scripts/publish_to_google_docs.py \
  --title "Research - <topic>" \
  --input-file /tmp/research_report.md
```

Optional:
- `--share-anyone-reader` to make read-only link public
- `--folder-id <GOOGLE_DRIVE_FOLDER_ID>` to place doc into a specific folder
- `--credentials /path/to/service-account.json` to override env var

6. Reply to user:
- short conclusion
- Google Docs URL
- top 3 insights

## Google Docs Setup

The script supports service account auth.

Environment:
- `GOOGLE_SERVICE_ACCOUNT_FILE=/path/to/service-account.json`
- (optional) `GOOGLE_DRIVE_FOLDER_ID=<folder_id>`

If dependencies are missing:
```bash
pip install google-api-python-client google-auth
```
