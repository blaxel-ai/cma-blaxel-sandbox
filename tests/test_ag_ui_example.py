import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1] / "example" / "ag-ui"


def test_dependencies_are_exactly_pinned():
    package = json.loads((ROOT / "package.json").read_text())
    for group in ("dependencies", "devDependencies", "overrides"):
        assert all(not version.startswith(("^", "~")) for version in package[group].values())
    assert package["overrides"]["rxjs"] == "7.8.1"


def test_every_ag_ui_session_is_budgeted_and_attributable():
    runtime = (ROOT / "runtime.ts").read_text()
    assert "max_list_cost" in runtime
    assert "cookbook: 'blaxel-cma'" in runtime
    assert "surface: 'ag-ui'" in runtime
    assert "...params.metadata" in runtime


def test_runtime_validates_spend_timeout_and_agent_version_inputs():
    runtime = (ROOT / "runtime.ts").read_text()
    assert "positiveInteger('AG_UI_BUDGET_CENTS'" in runtime
    assert "positiveInteger('AG_UI_TURN_TIMEOUT_MS'" in runtime
    assert "positiveInteger('ANTHROPIC_AGENT_VERSION'" in runtime


def test_unauthenticated_runtime_refuses_non_loopback_bindings():
    config = (ROOT / "vite.config.ts").read_text()
    assert "isLoopback" in config
    assert "may only bind to localhost" in config


def test_runtime_never_reads_worker_credentials():
    runtime = (ROOT / "runtime.ts").read_text()
    assert "ANTHROPIC_ENVIRONMENT_KEY" not in runtime
    assert "BL_API_KEY" not in runtime


def test_example_reuses_upstream_adapter_instead_of_an_agent_loop():
    runtime = (ROOT / "runtime.ts").read_text()
    assert "@ag-ui/claude-managed-agents" in runtime
    assert "sessions.events.stream" not in runtime
