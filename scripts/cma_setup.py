#!/usr/bin/env python3
"""Small shared helpers for the CMA cookbook setup scripts."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from typing import Any

ANTHROPIC_VERSION = "2023-06-01"
CMA_BETA = "managed-agents-2026-04-01"
DEFAULT_BASE_URL = "https://api.anthropic.com"

DEFAULT_AGENT_SYSTEM = (
    "You are a coding agent. Your working directory is /workspace. The file tools "
    "(write/read/edit/glob/grep) are sandboxed to /workspace; absolute paths like "
    "/workspace/hello.txt are REJECTED with \"absolute path not permitted\". Always "
    "pass bare relative paths to file tools (\"hello.txt\", not \"/workspace/hello.txt\"). "
    "Shell (bash) commands are unrestricted and use /workspace/... paths. Every tool "
    "call must produce non-empty output: if a shell command would print nothing "
    "(for example output redirected to a file), append a status echo such as && echo ok, "
    "because an empty tool result is rejected by the API."
)
DEFAULT_AGENT_MODEL = "claude-sonnet-5"


class SetupError(RuntimeError):
    """Raised for setup failures that should print as one readable line."""


def env(name: str, *, default: str | None = None, required: bool = False) -> str | None:
    value = os.environ.get(name, default)
    if required and not value:
        raise SetupError(f"missing required env: {name}")
    return value


def anthropic_headers(api_key: str) -> dict[str, str]:
    return {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "anthropic-beta": CMA_BETA,
        "content-type": "application/json",
    }


def request_json(method: str, path: str, *, body: dict[str, Any] | None = None) -> tuple[int, Any]:
    api_key = env("ANTHROPIC_API_KEY", required=True)
    base_url = env("ANTHROPIC_BASE_URL", default=DEFAULT_BASE_URL)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=data,
        headers=anthropic_headers(str(api_key)),
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read().decode("utf-8")
            return response.status, json.loads(raw or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload: Any = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"error": raw}
        return exc.code, payload
    except urllib.error.URLError as exc:
        raise SetupError(f"request failed: {exc.reason}") from exc


def extract_id(payload: Any, prefix: str) -> str:
    if isinstance(payload, dict) and isinstance(payload.get("id"), str):
        value = payload["id"]
        if value.startswith(prefix):
            return value
    raise SetupError(f"API response did not contain a {prefix}... id: {json.dumps(payload)[:500]}")


def print_export(name: str, value: str) -> None:
    print(f"export {name}={value}")


def environment_payload(name: str) -> dict[str, Any]:
    return {"name": name, "config": {"type": "self_hosted"}}


def agent_payload(
    name: str,
    model: str,
    *,
    inference_geo: str | None = None,
    advisor_model: str | None = None,
    skills: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    if inference_geo not in (None, "global", "us"):
        raise SetupError("ANTHROPIC_INFERENCE_GEO must be 'global' or 'us'")

    payload: dict[str, Any] = {
        "name": name,
        "model": {"id": model, "inference_geo": inference_geo} if inference_geo else model,
        "system": DEFAULT_AGENT_SYSTEM,
        "tools": [{"type": "agent_toolset_20260401"}],
        "description": "A production-ready coding agent that executes inside a Blaxel sandbox.",
        "metadata": {"cookbook": "blaxel-cma"},
    }
    if skills:
        payload["skills"] = skills
    if advisor_model:
        payload["multiagent"] = {
            "type": "coordinator",
            "agents": [
                {"type": "self"},
                {"type": "advisor", "model": advisor_model},
            ],
        }
    return payload


def parse_agent_skills(value: str | None) -> list[dict[str, str]]:
    """Parse `xlsx,skill_abc@3` into Managed Agents skill entries."""
    skills: list[dict[str, str]] = []
    for raw in (value or "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        skill_id, separator, version = raw.partition("@")
        if not skill_id:
            raise SetupError("ANTHROPIC_AGENT_SKILLS contains an empty skill id")
        item = {
            "type": "custom" if skill_id.startswith("skill_") else "anthropic",
            "skill_id": skill_id,
        }
        if separator:
            if not version:
                raise SetupError(f"skill {skill_id!r} has an empty version")
            item["version"] = version
        skills.append(item)
    return skills


def command_exists(command: str) -> bool:
    return shutil.which(command) is not None


def command_ok(args: list[str]) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    output = (result.stdout or result.stderr or "").strip()
    return result.returncode == 0, output


def run_main(main) -> None:
    try:
        main()
    except SetupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
