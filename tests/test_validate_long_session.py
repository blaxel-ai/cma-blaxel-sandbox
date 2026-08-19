from example.validate_long_session import containment_refused, long_run_checks


def test_containment_requires_error_for_the_exact_outside_write():
    events = [
        {
            "id": "tool_1",
            "type": "agent.tool_use",
            "name": "write",
            "input": {"file_path": "/tmp/cma-escape-probe.txt", "content": "x"},
        },
        {
            "id": "result_1",
            "type": "user.tool_result",
            "tool_use_id": "tool_1",
            "is_error": True,
        },
    ]
    assert containment_refused(events) is True


def test_containment_does_not_pass_on_agent_claim_alone():
    events = [{
        "id": "tool_1",
        "type": "agent.tool_use",
        "name": "write",
        "input": {"file_path": "/tmp/cma-escape-probe.txt"},
    }]
    assert containment_refused(events) is False


def test_long_run_checks_require_all_delayed_tools_and_containment():
    events = []
    for letter in "abc":
        events.append({
            "id": f"bash_{letter}",
            "type": "agent.tool_use",
            "name": "bash",
            "input": {"command": f"sleep 30 && echo {letter.upper()} > /workspace/{letter}.txt"},
        })
    events.extend([
        {"id": "outside", "type": "agent.tool_use", "name": "write", "input": {"file_path": "/tmp/cma-escape-probe.txt"}},
        {"id": "result", "type": "agent.tool_result", "tool_use_id": "outside", "is_error": True},
    ])
    assert all(long_run_checks(events, "COMBINED=ABC").values())
