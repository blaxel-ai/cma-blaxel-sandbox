from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(name: str) -> str:
    return (ROOT / name).read_text()


def test_public_docs_use_sonnet_5_as_the_default():
    assert "Default model: `claude-sonnet-5`" in read("llms.txt")
    assert "default is `claude-sonnet-5`" in read("AGENTS.md")
    assert "| Model | `claude-sonnet-5` |" in read("README.md")


def test_public_docs_do_not_offer_unsupported_self_hosted_resources():
    combined = "\n".join(read(name) for name in ("README.md", "GUIDE.md", "AGENTS.md", "llms.txt"))
    assert "--github-repository-url" not in combined
    assert "ANTHROPIC_GITHUB_TOKEN" not in combined
    assert "Self-hosted sessions reject" in combined or "Self-hosted sessions do not accept" in combined


def test_readme_embeds_the_checked_in_architecture_image():
    assert "assets/cma-blaxel-sandbox-flow.png" in read("README.md")
    assert (ROOT / "assets" / "cma-blaxel-sandbox-flow.png").is_file()


def test_docs_show_plan_before_cleanup_apply():
    readme = read("README.md")
    plan = "python3 cookbook.py cleanup --session sesn_..."
    apply = "python3 cookbook.py cleanup --session sesn_... --apply"
    assert readme.index(plan) < readme.index(apply)


def test_locked_install_is_the_primary_install_path():
    assert "--require-hashes -r requirements-dev.lock" in read("README.md")
    assert "--require-hashes -r requirements-dev.lock" in read("AGENTS.md")
    assert "--require-hashes -r requirements-dev.lock" in read("requirements-dev.txt")


def test_worker_verifies_the_anthropic_cli_archive():
    dockerfile = read("worker/Dockerfile")
    assert "ANT_SHA256_AMD64=" in dockerfile
    assert "ANT_SHA256_ARM64=" in dockerfile
    assert "sha256sum -c -" in dockerfile
