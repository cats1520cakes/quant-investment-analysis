from __future__ import annotations

import os
import subprocess


PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "SOCKS_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "socks_proxy",
)


class ProxyDetectedError(RuntimeError):
    """Raised when a data download would use a visible proxy path."""


def clear_proxy_environment() -> dict[str, str]:
    removed: dict[str, str] = {}
    for key in PROXY_ENV_KEYS:
        value = os.environ.pop(key, None)
        if value is not None:
            removed[key] = value
    os.environ["NO_PROXY"] = "*"
    os.environ["no_proxy"] = "*"
    return removed


def macos_proxy_state() -> dict[str, str]:
    try:
        result = subprocess.run(
            ["scutil", "--proxy"],
            text=True,
            capture_output=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    state: dict[str, str] = {}
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if " : " not in line:
            continue
        key, value = line.split(" : ", 1)
        state[key.strip()] = value.strip()
    return state


def visible_proxy_summary() -> list[str]:
    summary: list[str] = []
    for key in PROXY_ENV_KEYS:
        value = os.environ.get(key)
        if value:
            summary.append(f"{key}={value}")
    state = macos_proxy_state()
    if state.get("HTTPEnable") == "1":
        summary.append(f"macOS HTTP proxy {state.get('HTTPProxy', '')}:{state.get('HTTPPort', '')}")
    if state.get("HTTPSEnable") == "1":
        summary.append(f"macOS HTTPS proxy {state.get('HTTPSProxy', '')}:{state.get('HTTPSPort', '')}")
    if state.get("SOCKSEnable") == "1":
        summary.append(f"macOS SOCKS proxy {state.get('SOCKSProxy', '')}:{state.get('SOCKSPort', '')}")
    if state.get("ProxyAutoConfigEnable") == "1":
        summary.append("macOS PAC proxy enabled")
    if state.get("ProxyAutoDiscoveryEnable") == "1":
        summary.append("macOS proxy auto-discovery enabled")
    return summary


def require_direct_network(allow_proxy: bool = False) -> None:
    removed = clear_proxy_environment()
    visible = visible_proxy_summary()
    if allow_proxy:
        return
    if removed or visible:
        details = "; ".join([*(f"removed {key}" for key in removed), *visible])
        raise ProxyDetectedError(
            "Visible proxy settings detected; refusing market-data download to avoid VPN/proxy traffic. "
            "Disable the system proxy/VPN route first, or pass --allow-proxy only if you intentionally accept proxy traffic. "
            f"Detected: {details}"
        )
