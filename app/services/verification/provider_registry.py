"""Typed declarations for the bounded verification-provider registry."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderDescriptor:
    provider_id: str
    display_name: str
    adapter_version: str
    enabled: bool
    single_address: bool
    explicit_catch_all: bool
    domain_probe: bool
    balance_lookup: bool
    usage_lookup: bool
    simulator: bool
    timeout_seconds: int
    safe_retry_classes: tuple[str, ...]
    required_configuration: tuple[str, ...]


PROVIDERS: dict[str, ProviderDescriptor] = {
    "millionverifier": ProviderDescriptor(
        provider_id="millionverifier",
        display_name="MillionVerifier",
        adapter_version="millionverifier-single/v3",
        enabled=True,
        single_address=True,
        explicit_catch_all=True,
        domain_probe=False,
        balance_lookup=True,
        usage_lookup=False,
        simulator=True,
        timeout_seconds=20,
        safe_retry_classes=("transport", "rate_limit", "provider_5xx"),
        required_configuration=("api_key",),
    ),
    "debounce": ProviderDescriptor(
        provider_id="debounce",
        display_name="DeBounce",
        adapter_version="debounce-single/v1",
        enabled=True,
        single_address=True,
        explicit_catch_all=True,
        domain_probe=True,
        balance_lookup=True,
        usage_lookup=True,
        simulator=True,
        timeout_seconds=20,
        safe_retry_classes=("transport", "rate_limit", "provider_5xx"),
        required_configuration=("api_key",),
    ),
}


def descriptor(provider_id: str) -> ProviderDescriptor:
    try:
        return PROVIDERS[provider_id]
    except KeyError as exc:
        raise ValueError("unknown verification provider") from exc
