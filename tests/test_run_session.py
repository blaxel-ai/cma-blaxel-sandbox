import importlib
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "example"))

run_session = importlib.import_module("run_session")


def test_proof_preflight_allows_quiet_environment(monkeypatch):
    monkeypatch.setattr(
        run_session,
        "queue_stats",
        lambda: {"depth": 0, "pending": 0, "workers_polling": 0},
    )

    run_session.require_quiet_proof_environment()


def test_proof_preflight_rejects_existing_pollers(monkeypatch):
    monkeypatch.setattr(
        run_session,
        "queue_stats",
        lambda: {"depth": 0, "pending": 0, "workers_polling": 2},
    )

    with pytest.raises(SystemExit) as exc:
        run_session.require_quiet_proof_environment()

    message = str(exc.value)
    assert "workers_polling=2" in message
    assert "fresh environment" in message


def test_proof_preflight_rejects_existing_work(monkeypatch):
    monkeypatch.setattr(
        run_session,
        "queue_stats",
        lambda: {"depth": 1, "pending": 1, "workers_polling": 0},
    )

    with pytest.raises(SystemExit) as exc:
        run_session.require_quiet_proof_environment()

    message = str(exc.value)
    assert "depth=1" in message
    assert "pending=1" in message
