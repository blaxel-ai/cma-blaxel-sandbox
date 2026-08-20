from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(name: str) -> str:
    return (ROOT / name).read_text()


def test_public_docs_use_sonnet_5_as_the_default():
    assert "Default model: `claude-sonnet-5`" in read("llms.txt")
    assert "default is `claude-sonnet-5`" in read("AGENTS.md")
    assert "| Model | `claude-sonnet-5` |" in read("README.md")


def test_public_docs_distinguish_supported_self_hosted_resources():
    combined = "\n".join(read(name) for name in ("README.md", "GUIDE.md", "AGENTS.md", "llms.txt"))
    assert "memory_store" in combined
    assert "memory stores" in combined
    assert "do not mount uploaded files or GitHub repositories" in combined


def test_readme_embeds_the_checked_in_architecture_image():
    readme = read("README.md")
    assert "assets/cma-blaxel-sandbox-flow.png" in readme
    assert (ROOT / "assets" / "cma-blaxel-sandbox-flow.png").is_file()
    assert "Watch the 4K MP4" not in readme


def test_docs_show_plan_before_cleanup_apply():
    readme = read("README.md")
    plan = "python3 cookbook.py cleanup --session sesn_..."
    apply = "python3 cookbook.py cleanup --session sesn_... --apply"
    assert readme.index(plan) < readme.index(apply)


def test_locked_install_is_the_primary_install_path():
    assert "--require-hashes -r requirements-dev.lock" in read("README.md")
    assert "--require-hashes -r requirements-dev.lock" in read("AGENTS.md")
    assert "--require-hashes -r requirements-dev.lock" in read("requirements-dev.txt")


def test_worker_pins_the_sdk_environment_worker():
    dockerfile = read("worker/Dockerfile")
    worker = read("worker/worker.py")
    assert "--require-hashes -r /worker/requirements.lock" in dockerfile
    assert "anthropic>=1,<2" in read("worker/requirements.txt")
    assert "EnvironmentWorker" in worker
    assert "worker.handle_item(" in worker
    assert "work_secret=work_secret" in worker
