#!/usr/bin/env python3
"""Create the default Claude Managed Agent for this Blaxel self-hosted cookbook."""
from __future__ import annotations

from cma_setup import agent_payload, extract_id, env, print_export, request_json, run_main, SetupError


def main() -> None:
    name = env("ANTHROPIC_AGENT_NAME", default="Coding Assistant")
    model = env("ANTHROPIC_AGENT_MODEL", default="claude-opus-4-7")
    status, payload = request_json("POST", "/v1/agents", body=agent_payload(str(name), str(model)))
    if status >= 300:
        raise SetupError(f"agent create failed with HTTP {status}: {payload}")
    print_export("ANTHROPIC_AGENT_ID", extract_id(payload, "agent_"))


if __name__ == "__main__":
    run_main(main)
