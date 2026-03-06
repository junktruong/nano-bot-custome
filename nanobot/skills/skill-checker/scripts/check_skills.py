#!/usr/bin/env python3
"""Inspect discovered skills for current nanobot workspace."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any


def _parse_nanobot_metadata(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            return {}
        if isinstance(parsed.get("nanobot"), dict):
            return parsed["nanobot"]
        if isinstance(parsed.get("openclaw"), dict):
            return parsed["openclaw"]
    except Exception:
        pass
    return {}


def _missing_requirements(meta: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    requires = meta.get("requires", {}) if isinstance(meta, dict) else {}

    bins = requires.get("bins", []) if isinstance(requires, dict) else []
    for name in bins if isinstance(bins, list) else []:
        tool = str(name).strip()
        if tool and not shutil.which(tool):
            missing.append(f"bin:{tool}")

    envs = requires.get("env", []) if isinstance(requires, dict) else []
    for key in envs if isinstance(envs, list) else []:
        env_key = str(key).strip()
        if env_key and not os.environ.get(env_key):
            missing.append(f"env:{env_key}")
    return missing


def _collect(workspace_override: str, only: str) -> dict[str, Any]:
    workspace = _resolve_workspace(workspace_override)
    loader = _try_build_loader(workspace)

    if loader is not None:
        all_rows = loader.list_skills(filter_unavailable=False)
        available_names = {row["name"] for row in loader.list_skills(filter_unavailable=True)}
    else:
        all_rows = _scan_workspace_skills(workspace)
        available_names = {row["name"] for row in all_rows}

    needle = (only or "").strip().lower()

    items: list[dict[str, Any]] = []
    for row in sorted(all_rows, key=lambda x: x["name"]):
        name = row["name"]
        if needle and needle not in name.lower():
            continue

        front = (loader.get_skill_metadata(name) if loader is not None else _read_skill_frontmatter(Path(row["path"]))) or {}
        desc = str(front.get("description", "")).strip()
        skill_meta = _parse_nanobot_metadata(str(front.get("metadata", "")).strip())
        missing = _missing_requirements(skill_meta)
        available = name in available_names and not missing

        items.append(
            {
                "name": name,
                "source": row.get("source", ""),
                "path": row.get("path", ""),
                "available": available,
                "description": desc,
                "missing_requirements": missing,
            }
        )

    total = len(items)
    available_count = sum(1 for it in items if it["available"])
    unavailable_count = total - available_count
    return {
        "workspace": str(workspace),
        "total": total,
        "available": available_count,
        "unavailable": unavailable_count,
        "skills": items,
    }


def _resolve_workspace(workspace_override: str) -> Path:
    if workspace_override:
        return Path(workspace_override).expanduser()
    config_path = Path.home() / ".nanobot" / "config.json"
    if config_path.is_file():
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
            agents = data.get("agents", {}) if isinstance(data, dict) else {}
            defaults = agents.get("defaults", {}) if isinstance(agents, dict) else {}
            raw = str(defaults.get("workspace", "")).strip()
            if raw:
                return Path(raw).expanduser()
        except Exception:
            pass
    return Path.home() / ".nanobot" / "workspace"


def _try_build_loader(workspace: Path):
    try:
        from nanobot.agent.skills import SkillsLoader  # type: ignore

        return SkillsLoader(workspace)
    except Exception:
        # Try repository root nearby (for direct script usage in source tree).
        here = Path(__file__).resolve()
        for parent in here.parents:
            if (parent / "nanobot" / "agent" / "skills.py").is_file():
                if str(parent) not in sys.path:
                    sys.path.insert(0, str(parent))
                try:
                    from nanobot.agent.skills import SkillsLoader  # type: ignore

                    return SkillsLoader(workspace)
                except Exception:
                    pass
        return None


def _scan_workspace_skills(workspace: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    root = workspace / "skills"
    if not root.is_dir():
        return out
    for skill_dir in sorted(root.iterdir(), key=lambda p: p.name):
        if not skill_dir.is_dir():
            continue
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.is_file():
            continue
        out.append(
            {
                "name": skill_dir.name,
                "source": "workspace",
                "path": str(skill_md),
            }
        )
    return out


def _read_skill_frontmatter(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return {}
    if not text.startswith("---"):
        return {}
    block = text.split("---", 2)
    if len(block) < 3:
        return {}
    raw = block[1]
    meta: dict[str, Any] = {}
    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        meta[key.strip()] = value.strip().strip('"\'')
    return meta


def _print_table(payload: dict[str, Any]) -> None:
    print(f"workspace: {payload['workspace']}")
    print(
        f"skills: total={payload['total']} available={payload['available']} "
        f"unavailable={payload['unavailable']}"
    )
    if not payload["skills"]:
        print("(no skills)")
        return

    for item in payload["skills"]:
        status = "available" if item["available"] else "unavailable"
        missing = ", ".join(item["missing_requirements"]) if item["missing_requirements"] else "-"
        print(f"- {item['name']} [{status}] source={item['source']} missing={missing}")
        print(f"  path: {item['path']}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Check nanobot skills")
    parser.add_argument("--workspace", default="", help="Override workspace path")
    parser.add_argument("--only", default="", help="Filter by skill name (substring)")
    parser.add_argument("--format", choices=["table", "json"], default="table")
    args = parser.parse_args()

    payload = _collect(workspace_override=args.workspace, only=args.only)
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False))
    else:
        _print_table(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
