import importlib
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "example"))

demo_preview_resume = importlib.import_module("demo_preview_resume")


def test_normalize_preview_url_accepts_protocol_relative_url():
    assert demo_preview_resume.normalize_preview_url("//abc.preview.bl.run") == (
        "https://abc.preview.bl.run/"
    )


def test_normalize_preview_url_accepts_host_only_url():
    assert demo_preview_resume.normalize_preview_url("abc.preview.bl.run/path") == (
        "https://abc.preview.bl.run/path/"
    )


def test_preview_token_headers_only_set_for_private_token():
    assert demo_preview_resume._preview_token_headers(None) == {}
    assert demo_preview_resume._preview_token_headers("tok_demo") == {
        "X-Blaxel-Preview-Token": "tok_demo",
    }


def test_parse_args_defaults_to_private_preview():
    args = demo_preview_resume.parse_args([])

    assert args.public_preview is False
    assert args.preview_token_ttl_minutes == 10
    assert args.print_preview_token is False
    assert args.budget_cents == 100


def test_parse_args_public_preview_is_explicit():
    assert demo_preview_resume.parse_args(["--public-preview"]).public_preview is True


def test_parse_args_validates_positive_private_preview_ttl(capsys):
    with pytest.raises(SystemExit):
        demo_preview_resume.parse_args(["--preview-token-ttl-minutes", "0"])
    assert "greater than 0" in capsys.readouterr().err
