#!/usr/bin/env python3
"""Create the default Claude Managed Agent for this Blaxel self-hosted cookbook."""
from __future__ import annotations

import json

from cma_setup import (
    DEFAULT_AGENT_MODEL,
    SetupError,
    agent_payload,
    env,
    extract_id,
    parse_agent_skills,
    print_export,
    request_json,
    run_main,
)


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
    model = env("ANTHROPIC_AGENT_MODEL", default=DEFAULT_AGENT_MODEL)
    inference_geo = env("ANTHROPIC_INFERENCE_GEO")
    advisor_model = env("ANTHROPIC_ADVISOR_MODEL")
    skills = parse_agent_skills(env("ANTHROPIC_AGENT_SKILLS"))
    status, payload = request_json(
        "POST",
        "/v1/agents",
        body=agent_payload(
            str(name),
            str(model),
            inference_geo=str(inference_geo) if inference_geo else None,
            advisor_model=str(advisor_model) if advisor_model else None,
            skills=skills,
        ),
    )
    if status >= 300:
        raise SetupError(format_agent_create_error(status, payload, str(model)))
    print_export("ANTHROPIC_AGENT_ID", extract_id(payload, "agent_"))


if __name__ == "__main__":
    run_main(main)
