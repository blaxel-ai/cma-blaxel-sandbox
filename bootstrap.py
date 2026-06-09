#!/usr/bin/env python3
"""Guided, autonomous-but-transparent setup for the CMA-on-Blaxel cookbook.

Runs everything it safely can without prompting, narrating each step, and stops
only at the two Anthropic Console gates -- generate the environment key, and
register the webhook -- where a human must act in a browser. Re-run after each
gate to continue where it left off.

Conveniences that kill the multi-step copy/paste churn:
  - It reads `.env` directly, so you do NOT need to `source .env` between runs.
    Keys you paste into `.env` are picked up on the next run.
  - Ids it creates (environment, agent) are appended back to `.env` (after a
    one-time `.env.bak` backup), so resources are never double-created on re-run.

Usage:
  python3 bootstrap.py            # run the flow up to the next gate
  python3 bootstrap.py --plan     # show current state + the next action; run nothing

The two Console steps are unavoidable: environment-key generation and webhook
registration are Console-only in Managed Agents, so no script can do them.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
ENV_PATH = REPO / ".env"

# The four credentials whose presence defines "where am I" in the flow.
ENV_ID = "ANTHROPIC_ENVIRONMENT_ID"
ENV_KEY = "ANTHROPIC_ENVIRONMENT_KEY"
AGENT_ID = "ANTHROPIC_AGENT_ID"
SIGNING_KEY = "ANTHROPIC_WEBHOOK_SIGNING_KEY"

_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


# --- .env read / merge / append (pure, unit-tested) ------------------------

def parse_env_text(text: str) -> dict[str, str]:
    """Parse `.env`-style text into a dict, tolerating `export `, quotes, comments."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _ENV_LINE.match(line)
        if not match:
            continue
        name, value = match.group(1), match.group(2)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[name] = value
    return values


def load_env_file(path: Path) -> dict[str, str]:
    return parse_env_text(path.read_text()) if path.exists() else {}


def merged_env(base: dict[str, str], path: Path) -> dict[str, str]:
    """`base` (usually os.environ) with non-empty `.env` values overlaid.

    Overlaying means a key freshly pasted into `.env` takes effect on the next
    run without re-sourcing the shell -- the core convenience of this script.
    """
    env = dict(base)
    for name, value in load_env_file(path).items():
        if value:
            env[name] = value
    return env


def extract_export(text: str, name: str) -> str | None:
    """Pull the value from an `export NAME=value` line in captured script output."""
    match = re.search(rf"^\s*(?:export\s+)?{re.escape(name)}=(\S+)", text, re.MULTILINE)
    return match.group(1) if match else None


def append_export(path: Path, name: str, value: str) -> bool:
    """Append `export NAME=value` to `.env`. No-op if already present with that value.

    Backs up `.env` to `.env.bak` once before the first modification. Returns
    True if a line was written.
    """
    if load_env_file(path).get(name) == value:
        return False
    text = path.read_text() if path.exists() else ""
    backup = path.with_name(path.name + ".bak")
    if path.exists() and not backup.exists():
        shutil.copy2(path, backup)
    prefix = "" if text == "" or text.endswith("\n") else "\n"
    with path.open("a") as handle:
        handle.write(f"{prefix}export {name}={value}\n")
    return True


# --- flow decision (pure, unit-tested) -------------------------------------

# Frontier ids, in order. "create_env"/"provision"/"finalize" are autonomous
# steps; "gate_env_key"/"gate_webhook" need a human in the Anthropic Console.
def decide(state: dict[str, bool]) -> str:
    if not state.get("env_id"):
        return "create_env"
    if not state.get("env_key"):
        return "gate_env_key"
    if not state.get("agent_id"):
        return "provision"
    if not state.get("signing_key"):
        return "gate_webhook"
    return "finalize"


def state_from_env(env: dict[str, str]) -> dict[str, bool]:
    return {
        "env_id": bool(env.get(ENV_ID)),
        "env_key": bool(env.get(ENV_KEY)),
        "agent_id": bool(env.get(AGENT_ID)),
        "signing_key": bool(env.get(SIGNING_KEY)),
    }


# --- runtime wiring (thin; not the part under unit test) -------------------

def _run(cmd: list[str], env: dict[str, str], cwd: Path | None = None) -> int:
    print(f"   $ {' '.join(cmd)}")
    return subprocess.run(cmd, env=env, cwd=str(cwd) if cwd else None).returncode


def _capture(cmd: list[str], env: dict[str, str]) -> tuple[int, str]:
    print(f"   $ {' '.join(cmd)}")
    result = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
    if result.returncode != 0 and result.stderr:
        print(result.stderr, end="" if result.stderr.endswith("\n") else "\n")
    return result.returncode, result.stdout


def _die(message: str) -> None:
    print(f"\nstopped: {message}")
    raise SystemExit(1)


def _print_state(env: dict[str, str]) -> None:
    rows = [
        ("environment", ENV_ID, env.get(ENV_ID, "")),
        ("environment key", ENV_KEY, "set" if env.get(ENV_KEY) else ""),
        ("agent", AGENT_ID, env.get(AGENT_ID, "")),
        ("webhook signing key", SIGNING_KEY, "set" if env.get(SIGNING_KEY) else ""),
    ]
    for label, _key, detail in rows:
        marker = "ok " if detail else "-- "
        print(f"  {marker} {label}: {detail or 'not set'}")


def _gate_env_key() -> None:
    print(
        "\n>> NEXT -- Anthropic Console (generate the environment key):\n"
        "   1. Open https://platform.claude.com, select the workspace/project for\n"
        "      your ANTHROPIC_API_KEY, then open Managed Agents > Environments.\n"
        f"   2. Choose the environment id shown above ({ENV_ID}=env_...).\n"
        "   3. Click \"Generate environment key\".\n"
        f"   4. Add it to .env:   export {ENV_KEY}=sk-ant-oat01-...\n"
        "   5. Re-run:           python3 bootstrap.py\n"
        "   (No `source .env` needed -- bootstrap reads .env directly.)"
    )


def _gate_webhook() -> None:
    print(
        "\n>> NEXT -- Anthropic Console (register the webhook):\n"
        "   1. Open https://platform.claude.com, select the same workspace/project,\n"
        "      then create a Managed Agents webhook.\n"
        "   2. Subscribe only to `session.status_run_started`.\n"
        "   3. Set the destination to the exact URL setup.py printed above\n"
        "      (https://<id>.preview.bl.run/webhook).\n"
        f"   4. Add the one-time signing secret to .env:   export {SIGNING_KEY}=whsec_...\n"
        "   5. Re-run:                                      python3 bootstrap.py"
    )


def _do_create_env(env: dict[str, str]) -> None:
    print("\n-> creating the self-hosted environment ...")
    _, out = _capture([sys.executable, str(REPO / "scripts" / "create_environment.py")], env)
    value = extract_export(out, ENV_ID)
    if not value:
        _die("could not read the new environment id from create_environment.py output")
    if append_export(ENV_PATH, ENV_ID, value):
        print(f"   saved {ENV_ID} to .env")


def _do_provision(env: dict[str, str]) -> None:
    workspace = env.get("BL_WORKSPACE")
    if not workspace:
        _die("BL_WORKSPACE is not set")

    print("\n-> publishing the worker image (one-time, ~3 GB upload; this can take a few minutes) ...")
    if _run(["bl", "push", "--workspace", workspace, "--type", "sandbox"], env, cwd=REPO / "worker") != 0:
        _die("worker image push failed")

    print("\n-> creating the agent ...")
    _, out = _capture([sys.executable, str(REPO / "scripts" / "create_agent.py")], env)
    value = extract_export(out, AGENT_ID)
    if not value:
        _die("could not read the new agent id from create_agent.py output")
    if append_export(ENV_PATH, AGENT_ID, value):
        print(f"   saved {AGENT_ID} to .env")
    env = merged_env(env, ENV_PATH)  # so the proof below sees the new agent id

    print("\n-> proving the worker (creates a real session + worker sandbox) ...")
    if _run([sys.executable, str(REPO / "example" / "run_session.py"), "--direct-dispatch"], env) != 0:
        # The webhook path builds on a proven worker; setting it up anyway buries
        # the real failure under confusing downstream ones.
        _die(
            "worker proof did not pass -- fix it using the transcript above and the "
            "README 'Debug Fast' table, then re-run bootstrap."
        )

    print("\n-> publishing the orchestrator and starting its webhook server ...")
    if _run(["bl", "push", "--workspace", workspace, "--type", "sandbox"], env, cwd=REPO / "orchestrator") != 0:
        _die("orchestrator image push failed")
    if _run([sys.executable, str(REPO / "setup.py")], env) != 0:
        _die("orchestrator setup failed")


def _do_finalize(env: dict[str, str]) -> None:
    print("\n-> applying the webhook signing key (restarting the orchestrator) ...")
    if _run([sys.executable, str(REPO / "setup.py")], env) != 0:
        _die("orchestrator setup failed")
    print("\n-> proving the webhook path (creates a real session) ...")
    _run([sys.executable, str(REPO / "example" / "run_session.py")], env)


def main(argv: list[str]) -> int:
    import os

    # Line-buffer our own output so it interleaves correctly with child-process
    # output when piped (CI, tee, an agent capturing logs); a TTY already does this.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass

    plan = "--plan" in argv
    env = merged_env(dict(os.environ), ENV_PATH)

    print("Bootstrap: Claude Managed Agents on Blaxel\n")
    _print_state(env)

    if plan:
        nxt = {
            "create_env": "create the self-hosted environment",
            "gate_env_key": "generate the environment key in the Anthropic Console",
            "provision": "push the worker, create the agent, prove the worker, start the orchestrator",
            "gate_webhook": "register the webhook in the Anthropic Console",
            "finalize": "apply the signing key and prove the webhook path",
        }[decide(state_from_env(env))]
        print(f"\n>> next action: {nxt}")
        print("   (--plan shows the plan only; run `python3 bootstrap.py` to do it)")
        return 0

    print("\n-> checking access (preflight) ...")
    if _run([sys.executable, str(REPO / "scripts" / "preflight.py")], env) != 0:
        print("\npreflight failed -- fix the items above and re-run.")
        return 1

    while True:
        env = merged_env(dict(os.environ), ENV_PATH)
        step = decide(state_from_env(env))
        if step == "gate_env_key":
            _gate_env_key()
            return 0
        if step == "gate_webhook":
            _gate_webhook()
            return 0
        if step == "create_env":
            _do_create_env(env)
            continue
        if step == "provision":
            _do_provision(env)
            continue
        if step == "finalize":
            _do_finalize(env)
            print("\nSetup complete. The webhook path is live; re-run any proof from the README 'Tests' section.")
            return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
