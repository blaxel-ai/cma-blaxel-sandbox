#!/usr/bin/env python3
"""Create a Claude Managed Agents self-hosted environment."""
from __future__ import annotations

from cma_setup import environment_payload, extract_id, env, print_export, request_json, run_main, SetupError


def main() -> None:
    name = env("ANTHROPIC_ENVIRONMENT_NAME", default="blaxel-selfhosted")
    status, payload = request_json("POST", "/v1/environments", body=environment_payload(str(name)))
    if status >= 300:
        raise SetupError(f"environment create failed with HTTP {status}: {payload}")
    environment_id = extract_id(payload, "env_")
    print_export("ANTHROPIC_ENVIRONMENT_ID", environment_id)
    print("next: open the environment in the Anthropic Console and generate ANTHROPIC_ENVIRONMENT_KEY")


if __name__ == "__main__":
    run_main(main)
