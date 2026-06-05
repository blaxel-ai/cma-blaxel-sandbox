"""Blaxel-native worker features for the CMA cookbook.

These helpers keep optional sandbox features out of the default quickstart while
making them available to both the webhook orchestrator and the local-worker
proof path.
"""
from __future__ import annotations

import os
import re
import json
from typing import Mapping

from blaxel.core import VolumeInstance
from blaxel.core.client.api.configurations.get_configuration import asyncio as get_configuration
from blaxel.core.client.client import client as blaxel_client
from blaxel.core.client.errors import UnexpectedStatus

DEFAULT_VOLUME_MOUNT = "/workspace"
PROXY_REGION_CHECK_SKIP_ENV = "BLAXEL_WORKER_PROXY_SKIP_REGION_CHECK"


class BlaxelFeatureSetupError(RuntimeError):
    """Raised when an opt-in Blaxel feature cannot be provisioned."""


def _enabled(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _split_csv(value: str | None) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def _safe_name(value: str, *, max_len: int = 48) -> str:
    safe = re.sub(r"[^a-z0-9-]", "-", value.lower()).strip("-")
    return (safe or "session")[:max_len].strip("-") or "session"


def _int_env(environ: Mapping[str, str], name: str, default: int) -> int:
    raw = environ.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer number of MB") from exc
    if value <= 0:
        raise ValueError(f"{name} must be greater than 0")
    return value


def _summarize_api_error(content: bytes) -> str:
    try:
        decoded = content.decode("utf-8", errors="replace")
    except AttributeError:
        decoded = str(content)
    try:
        parsed = json.loads(decoded)
    except json.JSONDecodeError:
        return decoded.strip()
    if isinstance(parsed, dict):
        for key in ("error", "message"):
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return decoded.strip()


def _region_field(region: object, field: str):
    if isinstance(region, dict):
        return region.get(field) or region.get(_snake_to_camel(field))
    return getattr(region, field, None)


def _snake_to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


def _region_name(region: object) -> str | None:
    value = _region_field(region, "name")
    return value if isinstance(value, str) and value else None


def _region_proxy_available(region: object) -> bool:
    return _region_field(region, "proxy_available") is True


def _config_regions(configuration: object | None) -> list[object]:
    regions = getattr(configuration, "regions", None)
    if isinstance(configuration, dict):
        regions = configuration.get("regions")
    return regions if isinstance(regions, list) else []


async def ensure_proxy_region_supported(
    region: str | None,
    *,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Fail fast when public-preview Proxy is requested in an unsupported region."""
    if environ is None:
        environ = os.environ
    if _enabled(environ.get(PROXY_REGION_CHECK_SKIP_ENV)):
        return
    if not region:
        raise BlaxelFeatureSetupError(
            "Blaxel Proxy config requires BL_REGION so the cookbook can verify "
            "the worker sandbox region supports public-preview Proxy. Set "
            "BL_REGION to a proxy-supported region before enabling "
            "BLAXEL_WORKER_PROXY_DESTINATIONS, or set "
            f"{PROXY_REGION_CHECK_SKIP_ENV}=true only for offline experiments."
        )

    try:
        configuration = await get_configuration(client=blaxel_client)
    except UnexpectedStatus as exc:
        detail = _summarize_api_error(exc.content)
        raise BlaxelFeatureSetupError(
            f"Blaxel Proxy region check failed (HTTP {exc.status_code}): {detail}."
        ) from exc
    except Exception as exc:
        raise BlaxelFeatureSetupError(
            f"Blaxel Proxy region check failed: {exc!r}. Set "
            f"{PROXY_REGION_CHECK_SKIP_ENV}=true only if you have separately "
            "confirmed the worker region supports Proxy."
        ) from exc

    regions = _config_regions(configuration)
    match = next((item for item in regions if _region_name(item) == region), None)
    supported = sorted(
        name
        for item in regions
        if (name := _region_name(item)) and _region_proxy_available(item)
    )
    supported_text = ", ".join(supported) if supported else "none reported by /configuration"

    if match is None:
        raise BlaxelFeatureSetupError(
            f"Blaxel Proxy region check could not find BL_REGION={region!r}. "
            f"Proxy-supported regions: {supported_text}."
        )
    if not _region_proxy_available(match):
        raise BlaxelFeatureSetupError(
            f"Blaxel Proxy is not available in BL_REGION={region!r}. "
            f"Proxy-supported regions: {supported_text}."
        )


def worker_network_from_env(environ: Mapping[str, str] | None = None) -> dict | None:
    """Build optional public-preview Proxy config and domain filtering.

    Proxy secret injection is intentionally opt-in. The raw secret reaches the
    orchestrator so it can create/update the sandbox, but it is not passed to the
    worker process env where CMA tools run.
    """
    if environ is None:
        environ = os.environ
    destinations = _split_csv(environ.get("BLAXEL_WORKER_PROXY_DESTINATIONS"))
    allowed_domains = _split_csv(environ.get("BLAXEL_WORKER_PROXY_ALLOWED_DOMAINS"))
    forbidden_domains = _split_csv(environ.get("BLAXEL_WORKER_PROXY_FORBIDDEN_DOMAINS"))
    bypass = _split_csv(environ.get("BLAXEL_WORKER_PROXY_BYPASS"))
    secret_value = environ.get("BLAXEL_WORKER_PROXY_SECRET_VALUE")

    if not any((destinations, allowed_domains, forbidden_domains, bypass, secret_value)):
        return None
    if secret_value and not destinations:
        raise ValueError(
            "BLAXEL_WORKER_PROXY_DESTINATIONS is required when "
            "BLAXEL_WORKER_PROXY_SECRET_VALUE is set"
        )
    if destinations and not secret_value:
        raise ValueError(
            "BLAXEL_WORKER_PROXY_SECRET_VALUE is required when "
            "BLAXEL_WORKER_PROXY_DESTINATIONS is set"
        )

    proxy: dict = {"routing": []}
    if bypass:
        proxy["bypass"] = bypass

    if destinations:
        secret_name = environ.get("BLAXEL_WORKER_PROXY_SECRET_NAME", "api-token")
        header_name = environ.get("BLAXEL_WORKER_PROXY_HEADER_NAME", "Authorization")
        header_value = environ.get(
            "BLAXEL_WORKER_PROXY_HEADER_VALUE",
            f"Bearer {{{{SECRET:{secret_name}}}}}",
        )
        proxy["routing"].append({
            "destinations": destinations,
            "headers": {header_name: header_value},
            "secrets": {secret_name: secret_value},
        })

    network: dict = {"proxy": proxy}
    if allowed_domains:
        network["allowedDomains"] = allowed_domains
    if forbidden_domains:
        network["forbiddenDomains"] = forbidden_domains
    return network


def volume_plan_for_session(
    session_id: str,
    *,
    region: str | None,
    environ: Mapping[str, str] | None = None,
) -> tuple[dict, dict] | None:
    """Return (volume create config, sandbox attachment config) for a session."""
    if environ is None:
        environ = os.environ
    if not _enabled(environ.get("BLAXEL_WORKER_VOLUME_ENABLED")):
        return None
    if not region:
        raise ValueError(
            "BLAXEL_WORKER_VOLUME_ENABLED requires BL_REGION so the volume and "
            "worker sandbox are created in the same region"
        )

    prefix = _safe_name(environ.get("BLAXEL_WORKER_VOLUME_PREFIX", "cma-workspace"), max_len=32)
    name = f"{prefix}-{_safe_name(session_id)}"[:63].strip("-")
    mount_path = environ.get("BLAXEL_WORKER_VOLUME_MOUNT", DEFAULT_VOLUME_MOUNT)
    size = _int_env(environ, "BLAXEL_WORKER_VOLUME_SIZE_MB", 2048)

    create_config = {
        "name": name,
        "size": size,
        "region": region,
        "labels": {
            "app": "cma-blaxel-sandbox",
            "cma-session": _safe_name(session_id, max_len=32),
        },
    }
    attachment = {
        "name": name,
        "mount_path": mount_path,
        "read_only": False,
    }
    return create_config, attachment


async def apply_worker_features(
    spec: dict,
    *,
    session_id: str,
    region: str | None,
    environ: Mapping[str, str] | None = None,
) -> dict:
    """Return a worker sandbox spec with opt-in Blaxel features applied."""
    if environ is None:
        environ = os.environ
    spec = dict(spec)

    network = worker_network_from_env(environ)
    if network:
        await ensure_proxy_region_supported(region, environ=environ)
        spec["network"] = network

    volume_plan = volume_plan_for_session(session_id, region=region, environ=environ)
    if volume_plan:
        create_config, attachment = volume_plan
        try:
            await VolumeInstance.create_if_not_exists(create_config)
        except UnexpectedStatus as exc:
            detail = _summarize_api_error(exc.content)
            raise BlaxelFeatureSetupError(
                "Blaxel Volume setup failed for "
                f"{create_config['name']} (HTTP {exc.status_code}): {detail}. "
                "Disable BLAXEL_WORKER_VOLUME_ENABLED for the quickstart, or "
                "request Volume quota for this workspace before rerunning."
            ) from exc
        spec["volumes"] = [attachment]

    return spec
