"""Tests for bootstrap.py's pure decision and .env handling.

These lock the parts that make the guided flow safe and re-enterable: the
frontier decision (which step is next given current credentials), reading .env
directly so no reload is needed, and append-without-duplication so resources are
never double-created on re-run. Subprocess wiring is intentionally not exercised.
"""
import bootstrap


# --- decide(): the credential state determines the single next step ---------

def test_decide_walks_gates_in_order():
    assert bootstrap.decide({}) == "create_env"
    assert bootstrap.decide({"env_id": True}) == "gate_env_key"
    assert bootstrap.decide({"env_id": True, "env_key": True}) == "provision"
    assert bootstrap.decide({"env_id": True, "env_key": True, "agent_id": True}) == "gate_webhook"
    assert bootstrap.decide(
        {"env_id": True, "env_key": True, "agent_id": True, "signing_key": True}
    ) == "finalize"


def test_state_from_env_treats_empty_as_unset():
    env = {bootstrap.ENV_ID: "env_1", bootstrap.ENV_KEY: "", bootstrap.AGENT_ID: "agent_1"}
    state = bootstrap.state_from_env(env)
    assert state == {"env_id": True, "env_key": False, "agent_id": True, "signing_key": False}
    # an empty value must NOT advance the flow past the env-key gate
    assert bootstrap.decide(state) == "gate_env_key"


# --- .env parsing tolerates the formats the cookbook actually uses ----------

def test_parse_env_text_handles_export_quotes_and_comments():
    text = (
        "# comment\n"
        "export ANTHROPIC_ENVIRONMENT_ID=env_abc\n"
        "BL_WORKSPACE=main\n"
        'ANTHROPIC_ENVIRONMENT_KEY="sk-ant-oat01-x"\n'
        "\n"
        "   export ANTHROPIC_AGENT_ID = agent_z  \n"
    )
    parsed = bootstrap.parse_env_text(text)
    assert parsed["ANTHROPIC_ENVIRONMENT_ID"] == "env_abc"
    assert parsed["BL_WORKSPACE"] == "main"
    assert parsed["ANTHROPIC_ENVIRONMENT_KEY"] == "sk-ant-oat01-x"
    assert parsed["ANTHROPIC_AGENT_ID"] == "agent_z"


def test_merged_env_overlays_nonempty_dotenv_over_base(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("export ANTHROPIC_ENVIRONMENT_KEY=sk-ant-oat01-fresh\nANTHROPIC_AGENT_ID=\n")
    base = {"ANTHROPIC_ENVIRONMENT_KEY": "stale-or-empty", "ANTHROPIC_AGENT_ID": "agent_base", "BL_REGION": "us-pdx-1"}
    merged = bootstrap.merged_env(base, env_file)
    # a freshly pasted .env key wins without re-sourcing the shell
    assert merged["ANTHROPIC_ENVIRONMENT_KEY"] == "sk-ant-oat01-fresh"
    # an EMPTY .env value must not clobber a real base value
    assert merged["ANTHROPIC_AGENT_ID"] == "agent_base"
    # base-only keys survive
    assert merged["BL_REGION"] == "us-pdx-1"


def test_extract_export_reads_script_output():
    assert bootstrap.extract_export("export ANTHROPIC_ENVIRONMENT_ID=env_x\n", "ANTHROPIC_ENVIRONMENT_ID") == "env_x"
    assert bootstrap.extract_export("ANTHROPIC_AGENT_ID=agent_y", "ANTHROPIC_AGENT_ID") == "agent_y"
    assert bootstrap.extract_export("nothing here", "ANTHROPIC_ENVIRONMENT_ID") is None


# --- append_export(): the re-run safety net ---------------------------------

def test_append_export_writes_then_is_idempotent_and_backs_up(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("export ANTHROPIC_API_KEY=sk-ant-api03-x\n")

    assert bootstrap.append_export(env_file, "ANTHROPIC_ENVIRONMENT_ID", "env_new") is True
    assert "export ANTHROPIC_ENVIRONMENT_ID=env_new" in env_file.read_text()
    # a backup of the original was taken once
    assert (tmp_path / ".env.bak").exists()
    assert (tmp_path / ".env.bak").read_text() == "export ANTHROPIC_API_KEY=sk-ant-api03-x\n"

    # re-running with the same value does not duplicate the line (re-run safety)
    assert bootstrap.append_export(env_file, "ANTHROPIC_ENVIRONMENT_ID", "env_new") is False
    assert env_file.read_text().count("ANTHROPIC_ENVIRONMENT_ID=env_new") == 1


def test_append_export_inserts_newline_when_file_lacks_trailing_one(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("export ANTHROPIC_API_KEY=x")  # no trailing newline
    bootstrap.append_export(env_file, "ANTHROPIC_AGENT_ID", "agent_q")
    lines = env_file.read_text().splitlines()
    assert lines == ["export ANTHROPIC_API_KEY=x", "export ANTHROPIC_AGENT_ID=agent_q"]


def test_append_export_creates_file_when_missing(tmp_path):
    env_file = tmp_path / ".env"
    assert bootstrap.append_export(env_file, "ANTHROPIC_ENVIRONMENT_ID", "env_first") is True
    assert env_file.read_text() == "export ANTHROPIC_ENVIRONMENT_ID=env_first\n"
