---
name: skill-checker
description: Check which skills are currently discovered by nanobot agent, report available/unavailable status, paths, and missing requirements. Use when user asks to list skills, verify a skill exists, or diagnose why a skill is not triggered.
metadata: {"nanobot":{"emoji":"🧩","requires":{"bins":["python3"],"env":[]}}}
---

# Skill Checker

Use this skill when user asks:
- "agent có những skill nào?"
- "kiểm tra skill X có trong bot không"
- "vì sao skill không chạy"

## Script

Run:

`python3 nanobot/skills/skill-checker/scripts/check_skills.py --format table`

Optional filters:

- Check specific skill by name:
`python3 nanobot/skills/skill-checker/scripts/check_skills.py --only facebook-messenger-assist --format table`
- JSON output:
`python3 nanobot/skills/skill-checker/scripts/check_skills.py --format json`

## Response Rules

- Always show:
  - total skills
  - available count
  - unavailable count
- If a skill is unavailable, include missing requirements.
- If user asks to fix unavailable skills, propose exact install steps based on missing bins/env.
