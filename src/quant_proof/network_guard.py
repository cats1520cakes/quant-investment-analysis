from __future__ import annotations

import os
import socket
import subprocess
import urllib.request


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


_ORIGINAL_URLLIB_GETPROXIES = urllib.request.getproxies
_ORIGINAL_URLLIB_GETPROXIES_ENVIRONMENT = urllib.request.getproxies_environment
_ORIGINAL_SOCKET_TIMEOUT = socket.getdefaulttimeout()
_DIRECT_PATCH_ACTIVE = False


def clear_proxy_environment() -> dict[str, str]:
    removed: dict[str, str] = {}
    for key in PROXY_ENV_KEYS:
        value = os.environ.pop(key, None)
        if value is not None:
            removed[key] = value
    os.environ["NO_PROXY"] = "*"
    os.environ["no_proxy"] = "*"
    return removed


def disable_python_proxy_discovery() -> None:
    global _DIRECT_PATCH_ACTIVE
    urllib.request.getproxies = lambda: {}
    urllib.request.getproxies_environment = lambda: {}
    try:
        import requests.utils

        requests.utils.get_environ_proxies = lambda url, no_proxy=None: {}
        requests.utils.should_bypass_proxies = lambda url, no_proxy=None: True
    except Exception:
        pass
    _DIRECT_PATCH_ACTIVE = True


def restore_python_proxy_discovery() -> None:
    global _DIRECT_PATCH_ACTIVE
    urllib.request.getproxies = _ORIGINAL_URLLIB_GETPROXIES
    urllib.request.getproxies_environment = _ORIGINAL_URLLIB_GETPROXIES_ENVIRONMENT
    _DIRECT_PATCH_ACTIVE = False


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


def require_direct_network(allow_proxy: bool = False, socket_timeout_seconds: float = 30.0) -> list[str]:
    if allow_proxy:
        return visible_proxy_summary()
    clear_proxy_environment()
    visible = visible_proxy_summary()
    disable_python_proxy_discovery()
    socket.setdefaulttimeout(socket_timeout_seconds)
    return visible


def direct_network_message(visible: list[str]) -> str:
    if not visible:
        return "direct network mode active; no visible proxy settings detected"
    return "direct network mode active; bypassing visible proxy settings: " + "; ".join(visible)
