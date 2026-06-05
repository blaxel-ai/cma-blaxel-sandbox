import pytest

from orchestrator import blaxel_features


class _FakeConfig:
    def __init__(self, regions):
        self.regions = regions


class _FakeRegion:
    def __init__(self, name, proxy_available):
        self.name = name
        self.proxy_available = proxy_available


async def _configuration_with_regions(regions):
    async def _loader(*, client):
        return _FakeConfig(regions)

    return _loader


def test_worker_network_from_env_is_empty_by_default():
    assert blaxel_features.worker_network_from_env({}) is None


def test_worker_network_from_env_builds_proxy_secret_route():
    network = blaxel_features.worker_network_from_env({
        "BLAXEL_WORKER_PROXY_DESTINATIONS": "api.stripe.com, api.example.com",
        "BLAXEL_WORKER_PROXY_HEADER_NAME": "X-API-Key",
        "BLAXEL_WORKER_PROXY_SECRET_NAME": "demo-key",
        "BLAXEL_WORKER_PROXY_SECRET_VALUE": "sk_demo",
        "BLAXEL_WORKER_PROXY_ALLOWED_DOMAINS": "api.stripe.com,api.example.com",
        "BLAXEL_WORKER_PROXY_BYPASS": "*.s3.amazonaws.com",
    })

    assert network == {
        "allowedDomains": ["api.stripe.com", "api.example.com"],
        "proxy": {
            "bypass": ["*.s3.amazonaws.com"],
            "routing": [{
                "destinations": ["api.stripe.com", "api.example.com"],
                "headers": {"X-API-Key": "Bearer {{SECRET:demo-key}}"},
                "secrets": {"demo-key": "sk_demo"},
            }],
        },
    }


def test_worker_network_requires_destination_for_proxy_secret():
    with pytest.raises(ValueError, match="DESTINATIONS"):
        blaxel_features.worker_network_from_env({
            "BLAXEL_WORKER_PROXY_SECRET_VALUE": "sk_demo",
        })


def test_worker_network_requires_secret_for_proxy_destination():
    with pytest.raises(ValueError, match="SECRET_VALUE"):
        blaxel_features.worker_network_from_env({
            "BLAXEL_WORKER_PROXY_DESTINATIONS": "api.example.com",
        })


async def test_proxy_region_check_accepts_supported_sdk_region(monkeypatch):
    monkeypatch.setattr(
        blaxel_features,
        "get_configuration",
        await _configuration_with_regions([
            _FakeRegion("us-pdx-1", True),
        ]),
    )

    await blaxel_features.ensure_proxy_region_supported(
        "us-pdx-1",
        environ={"BLAXEL_WORKER_PROXY_DESTINATIONS": "api.example.com"},
    )


async def test_proxy_region_check_accepts_supported_dict_region(monkeypatch):
    monkeypatch.setattr(
        blaxel_features,
        "get_configuration",
        await _configuration_with_regions([
            {"name": "us-pdx-1", "proxyAvailable": True},
        ]),
    )

    await blaxel_features.ensure_proxy_region_supported("us-pdx-1", environ={})


async def test_proxy_region_check_requires_region_when_proxy_is_enabled():
    with pytest.raises(blaxel_features.BlaxelFeatureSetupError) as exc_info:
        await blaxel_features.ensure_proxy_region_supported(None, environ={})

    message = str(exc_info.value)
    assert "BL_REGION" in message
    assert "BLAXEL_WORKER_PROXY_DESTINATIONS" in message


async def test_proxy_region_check_rejects_unsupported_region(monkeypatch):
    monkeypatch.setattr(
        blaxel_features,
        "get_configuration",
        await _configuration_with_regions([
            _FakeRegion("us-pdx-1", False),
            _FakeRegion("us-was-1", True),
        ]),
    )

    with pytest.raises(blaxel_features.BlaxelFeatureSetupError) as exc_info:
        await blaxel_features.ensure_proxy_region_supported("us-pdx-1", environ={})

    message = str(exc_info.value)
    assert "not available" in message
    assert "us-pdx-1" in message
    assert "us-was-1" in message


async def test_proxy_region_check_rejects_unknown_region(monkeypatch):
    monkeypatch.setattr(
        blaxel_features,
        "get_configuration",
        await _configuration_with_regions([
            _FakeRegion("us-was-1", True),
        ]),
    )

    with pytest.raises(blaxel_features.BlaxelFeatureSetupError) as exc_info:
        await blaxel_features.ensure_proxy_region_supported("antarctica-1", environ={})

    assert "could not find" in str(exc_info.value)
    assert "us-was-1" in str(exc_info.value)


async def test_proxy_region_check_can_be_explicitly_skipped(monkeypatch):
    called = False

    async def _loader(*, client):
        nonlocal called
        called = True
        return _FakeConfig([])

    monkeypatch.setattr(blaxel_features, "get_configuration", _loader)

    await blaxel_features.ensure_proxy_region_supported(
        None,
        environ={"BLAXEL_WORKER_PROXY_SKIP_REGION_CHECK": "true"},
    )

    assert called is False


def test_volume_plan_is_empty_unless_enabled():
    assert blaxel_features.volume_plan_for_session(
        "sesn_abc",
        region="us-pdx-1",
        environ={},
    ) is None


def test_volume_plan_uses_per_session_workspace_volume():
    create_config, attachment = blaxel_features.volume_plan_for_session(
        "sesn_ABC/123",
        region="us-pdx-1",
        environ={
            "BLAXEL_WORKER_VOLUME_ENABLED": "true",
            "BLAXEL_WORKER_VOLUME_PREFIX": "cma-workspace",
            "BLAXEL_WORKER_VOLUME_SIZE_MB": "4096",
        },
    )

    assert create_config == {
        "name": "cma-workspace-sesn-abc-123",
        "size": 4096,
        "region": "us-pdx-1",
        "labels": {
            "app": "cma-blaxel-sandbox",
            "cma-session": "sesn-abc-123",
        },
    }
    assert attachment == {
        "name": "cma-workspace-sesn-abc-123",
        "mount_path": "/workspace",
        "read_only": False,
    }


def test_volume_plan_requires_region_when_enabled():
    with pytest.raises(ValueError, match="BL_REGION"):
        blaxel_features.volume_plan_for_session(
            "sesn_x",
            region=None,
            environ={"BLAXEL_WORKER_VOLUME_ENABLED": "true"},
        )


async def test_apply_worker_features_creates_volume_and_returns_sdk_shape(monkeypatch):
    calls = []

    class FakeVolumeInstance:
        @classmethod
        async def create_if_not_exists(cls, config):
            calls.append(config)

    monkeypatch.setattr(blaxel_features, "VolumeInstance", FakeVolumeInstance)
    monkeypatch.setattr(
        blaxel_features,
        "get_configuration",
        await _configuration_with_regions([
            _FakeRegion("us-pdx-1", True),
        ]),
    )

    spec = await blaxel_features.apply_worker_features(
        {"name": "worker", "image": "sandbox/cma-worker:latest"},
        session_id="sesn_x",
        region="us-pdx-1",
        environ={
            "BLAXEL_WORKER_VOLUME_ENABLED": "true",
            "BLAXEL_WORKER_PROXY_DESTINATIONS": "api.example.com",
            "BLAXEL_WORKER_PROXY_SECRET_VALUE": "secret",
        },
    )

    assert calls == [{
        "name": "cma-workspace-sesn-x",
        "size": 2048,
        "region": "us-pdx-1",
        "labels": {
            "app": "cma-blaxel-sandbox",
            "cma-session": "sesn-x",
        },
    }]
    assert spec["volumes"] == [{
        "name": "cma-workspace-sesn-x",
        "mount_path": "/workspace",
        "read_only": False,
    }]
    assert spec["network"]["proxy"]["routing"][0]["secrets"] == {"api-token": "secret"}


async def test_apply_worker_features_turns_volume_quota_error_into_setup_gate(monkeypatch):
    class FakeVolumeInstance:
        @classmethod
        async def create_if_not_exists(cls, config):
            raise blaxel_features.UnexpectedStatus(
                429,
                b'{"code":429,"error":"Quota exceeded: 0/0 volumes"}',
            )

    monkeypatch.setattr(blaxel_features, "VolumeInstance", FakeVolumeInstance)

    with pytest.raises(blaxel_features.BlaxelFeatureSetupError) as exc_info:
        await blaxel_features.apply_worker_features(
            {"name": "worker", "image": "sandbox/cma-worker:latest"},
            session_id="sesn_x",
            region="us-pdx-1",
            environ={"BLAXEL_WORKER_VOLUME_ENABLED": "true"},
        )

    message = str(exc_info.value)
    assert "Blaxel Volume setup failed" in message
    assert "Quota exceeded: 0/0 volumes" in message
    assert "BLAXEL_WORKER_VOLUME_ENABLED" in message


async def test_apply_worker_features_checks_proxy_region_before_applying_network(monkeypatch):
    monkeypatch.setattr(
        blaxel_features,
        "get_configuration",
        await _configuration_with_regions([
            _FakeRegion("us-pdx-1", False),
        ]),
    )

    with pytest.raises(blaxel_features.BlaxelFeatureSetupError, match="Proxy"):
        await blaxel_features.apply_worker_features(
            {"name": "worker", "image": "sandbox/cma-worker:latest"},
            session_id="sesn_x",
            region="us-pdx-1",
            environ={
                "BLAXEL_WORKER_PROXY_DESTINATIONS": "api.example.com",
                "BLAXEL_WORKER_PROXY_SECRET_VALUE": "secret",
            },
        )
