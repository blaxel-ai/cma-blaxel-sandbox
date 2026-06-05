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


def test_parse_args_defaults_to_public_preview():
    args = demo_preview_resume.parse_args([])

    assert args.private_preview is False
    assert args.preview_token_ttl_minutes == 10
    assert args.print_preview_token is False


def test_parse_args_validates_positive_private_preview_ttl():
    with pytest.raises(SystemExit, match="greater than 0"):
        demo_preview_resume.parse_args(["--private-preview", "--preview-token-ttl-minutes", "0"])
