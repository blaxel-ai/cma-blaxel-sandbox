import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import cma_setup
import create_agent
import preflight


def test_environment_payload_is_self_hosted():
    assert cma_setup.environment_payload("blaxel-selfhosted") == {
        "name": "blaxel-selfhosted",
        "config": {"type": "self_hosted"},
    }


def test_agent_payload_uses_builtin_agent_toolset_and_workspace_path_prompt():
    payload = cma_setup.agent_payload("Coding Assistant", "claude-opus-5")
    assert payload["name"] == "Coding Assistant"
    assert payload["model"] == "claude-opus-5"
    assert payload["tools"] == [{"type": "agent_toolset_20260401"}]
    assert "read/write/edit may use absolute paths only inside /workspace" in payload["system"]
    assert "Use relative glob patterns" in payload["system"]
    assert "Every tool call must produce non-empty output" in payload["system"]
    assert payload["metadata"] == {"cookbook": "blaxel-cma"}


def test_agent_payload_supports_pinned_inference_and_advisor():
    payload = cma_setup.agent_payload(
        "API Designer",
        "claude-sonnet-5",
        inference_geo="us",
        advisor_model="claude-opus-5",
    )

    assert payload["model"] == {"id": "claude-sonnet-5", "inference_geo": "us"}
    assert payload["multiagent"] == {
        "type": "coordinator",
        "agents": [
            {"type": "advisor", "model": "claude-opus-5"},
        ],
    }


def test_agent_payload_rejects_unknown_inference_geo():
    with pytest.raises(cma_setup.SetupError, match="global.*us"):
        cma_setup.agent_payload("Coding Assistant", "claude-opus-5", inference_geo="eu")


def test_sonnet_5_is_the_default_agent_model():
    assert cma_setup.DEFAULT_AGENT_MODEL == "claude-sonnet-5"


def test_parse_agent_skills_supports_anthropic_and_custom_versions():
    assert cma_setup.parse_agent_skills("xlsx, skill_abc@3") == [
        {"type": "anthropic", "skill_id": "xlsx"},
        {"type": "custom", "skill_id": "skill_abc", "version": "3"},
    ]


def test_agent_payload_attaches_skills():
    skills = [{"type": "anthropic", "skill_id": "pdf"}]
    payload = cma_setup.agent_payload("Reader", "claude-sonnet-5", skills=skills)
    assert payload["skills"] == skills


def test_extract_id_accepts_expected_prefix():
    assert cma_setup.extract_id({"id": "env_123"}, "env_") == "env_123"


def test_extract_id_rejects_missing_or_wrong_prefix():
    for payload in ({}, {"id": "agent_123"}):
        try:
            cma_setup.extract_id(payload, "env_")
        except cma_setup.SetupError as exc:
            assert "env_" in str(exc)
        else:
            raise AssertionError("expected SetupError")


def test_anthropic_headers_include_beta_and_content_type():
    headers = cma_setup.anthropic_headers("sk-test")
    assert headers["x-api-key"] == "sk-test"
    assert headers["anthropic-version"] == "2023-06-01"
    assert headers["anthropic-beta"] == "managed-agents-2026-04-01"
    assert headers["content-type"] == "application/json"


def test_preflight_summarizes_json_command_output():
    assert preflight._command_detail("[]") == "reachable (0 resources)"
    assert preflight._command_detail('[{"metadata": {"name": "one"}}]') == "reachable (1 resources)"
    assert preflight._command_detail('{"status": "ok"}') == "reachable"


def test_create_agent_exports_id_and_integer_version(monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_AGENT_ID", raising=False)
    monkeypatch.setattr(
        create_agent,
        "request_json",
        lambda *args, **kwargs: (200, {"id": "agent_x", "version": 7}),
    )
    create_agent.main()
    output = capsys.readouterr().out
    assert "export ANTHROPIC_AGENT_ID=agent_x" in output
    assert "export ANTHROPIC_AGENT_VERSION=7" in output


def test_create_agent_recovers_version_for_existing_agent(monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_AGENT_ID", "agent_existing")
    monkeypatch.setattr(
        create_agent,
        "request_json",
        lambda method, path, **kwargs: (200, {"id": "agent_existing", "version": 9}),
    )

    create_agent.main()

    output = capsys.readouterr().out
    assert "export ANTHROPIC_AGENT_ID=agent_existing" in output
    assert "export ANTHROPIC_AGENT_VERSION=9" in output


def test_create_agent_archives_incomplete_response(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_AGENT_ID", raising=False)
    calls = []

    def request(method, path, **kwargs):
        calls.append((method, path))
        if method == "POST":
            return 200, {"id": "agent_incomplete"}
        return 204, None

    monkeypatch.setattr(create_agent, "request_json", request)
    with pytest.raises(cma_setup.SetupError, match="agent_incomplete archived"):
        create_agent.main()
    assert calls[-1] == ("POST", "/v1/agents/agent_incomplete/archive")


def test_agent_create_error_hints_when_model_is_rejected():
    message = create_agent.format_agent_create_error(
        400,
        {"error": {"message": "model: unknown model"}},
        "claude-opus-4-8",
    )

    assert "ANTHROPIC_AGENT_MODEL" in message
    assert "claude-opus-4-8" in message
