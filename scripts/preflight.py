#!/usr/bin/env python3
"""Read-only preflight checks for the Blaxel CMA self-hosted cookbook."""
from __future__ import annotations

import importlib.util
import json
import os

from cma_setup import command_exists, command_ok, request_json, run_main, SetupError


def _status_line(ok: bool, label: str, detail: str = "") -> str:
    marker = "ok" if ok else "missing"
    return f"{marker:7} {label}{(': ' + detail) if detail else ''}"


def _require_env(name: str) -> tuple[bool, str]:
    value = os.environ.get(name)
    if not value:
        return False, "not set"
    return True, "set"


def _command_detail(detail: str) -> str:
    if not detail:
        return "reachable"
    first_line = detail.splitlines()[0]
    try:
        parsed = json.loads(detail)
    except json.JSONDecodeError:
        return first_line
    if isinstance(parsed, list):
        return f"reachable ({len(parsed)} resources)"
    if isinstance(parsed, dict):
        return "reachable"
    return first_line


def main() -> None:
    failures: list[str] = []

    for name in ("ANTHROPIC_API_KEY", "BL_WORKSPACE", "BL_API_KEY"):
        ok, detail = _require_env(name)
        print(_status_line(ok, name, detail))
        if not ok:
            failures.append(name)

    for command in ("python3", "bl", "docker"):
        ok = command_exists(command)
        print(_status_line(ok, command, "on PATH" if ok else "not on PATH"))
        if not ok:
            failures.append(command)

    ok = importlib.util.find_spec("blaxel") is not None
    print(_status_line(ok, "python package blaxel", "importable" if ok else "install blaxel>=0.2.54"))
    if not ok:
        failures.append("blaxel package")

    if command_exists("docker"):
        ok, detail = command_ok(["docker", "info"])
        print(_status_line(ok, "Docker daemon", detail.splitlines()[0] if detail else "reachable"))
        if not ok:
            failures.append("Docker daemon")

    workspace = os.environ.get("BL_WORKSPACE")
    if command_exists("bl") and workspace:
        ok, detail = command_ok(["bl", "get", "sandboxes", "--workspace", workspace, "-o", "json"])
        print(_status_line(ok, "Blaxel sandbox API", _command_detail(detail)))
        if not ok:
            failures.append("Blaxel sandbox API")

    if os.environ.get("ANTHROPIC_API_KEY"):
        status, payload = request_json("GET", "/v1/environments")
        ok = status == 200 and isinstance(payload, dict) and isinstance(payload.get("data"), list)
        print(_status_line(ok, "Anthropic CMA beta access", f"HTTP {status}"))
        if not ok:
            failures.append("Anthropic CMA beta access")

    if failures:
        raise SetupError("preflight failed: " + ", ".join(failures))
    print("preflight passed")


if __name__ == "__main__":
    run_main(main)
