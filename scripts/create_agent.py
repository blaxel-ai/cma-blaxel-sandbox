#!/usr/bin/env python3
"""Create the default Claude Managed Agent for this Blaxel self-hosted cookbook."""
from __future__ import annotations

import json

from cma_setup import agent_payload, extract_id, env, print_export, request_json, run_main, SetupError


def format_agent_create_error(status: int, payload: object, model: str) -> str:
    message = f"agent create failed with HTTP {status}: {payload}"
    try:
        payload_text = json.dumps(payload).lower()
    except TypeError:
        payload_text = str(payload).lower()
    if "model" in payload_text:
        message += (
            f". The model id {model!r} may be unavailable for this account; "
            "set ANTHROPIC_AGENT_MODEL to a current Managed Agents model id and rerun."
        )
    return message


def main() -> None:
    name = env("ANTHROPIC_AGENT_NAME", default="Coding Assistant")
    model = env("ANTHROPIC_AGENT_MODEL", default="claude-opus-4-8")
    status, payload = request_json("POST", "/v1/agents", body=agent_payload(str(name), str(model)))
    if status >= 300:
        raise SetupError(format_agent_create_error(status, payload, str(model)))
    print_export("ANTHROPIC_AGENT_ID", extract_id(payload, "agent_"))


if __name__ == "__main__":
    run_main(main)
