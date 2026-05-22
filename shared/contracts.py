PRODUCTION_PROFILE = {
    "ipv6_policy": "ipv6_only",
    "network_profile": "high_compatibility",
    "fingerprint_profile_version": "v2_android_ipv6_only_dns_custom",
    "intended_client_os_profile": "android_mobile",
    "client_os_profile_enforcement": "not_controlled_by_proxy",
    "actual_client_profile": "not_controlled_by_proxy",
    "effective_client_os_profile": "not_controlled_by_proxy",
}

DUALSTACK_PROFILE = {**PRODUCTION_PROFILE, "ipv6_policy": "strict_dual_stack"}


def profile_for_sku(sku: dict) -> dict:
    if str((sku or {}).get("product_kind") or "") == "dualstack":
        return DUALSTACK_PROFILE
    return PRODUCTION_PROFILE


def profile_for_product(product: str) -> dict:
    if str(product or "") == "dualstack_ipv6":
        return DUALSTACK_PROFILE
    return PRODUCTION_PROFILE

FORBIDDEN_JOB_FIELDS = {
    "actualClientProfile",
    "actual_client_profile",
    "clientOsProfile",
    "client_os_profile",
    "clientOsProfileEnforcement",
    "client_os_profile_enforcement",
    "effectiveClientOsProfile",
    "effective_client_os_profile",
    "effectiveIpv6Policy",
    "effective_ipv6_policy",
    "fingerprintProfileVersion",
    "fingerprint_profile_version",
    "generatorArgs",
    "generatorScript",
    "intendedClientOsProfile",
    "intended_client_os_profile",
    "ipv6Policy",
    "ipv6_policy",
    "networkProfile",
    "network_profile",
    "proxyType",
    "proxy_type",
    "startPort",
    "start_port",
}
