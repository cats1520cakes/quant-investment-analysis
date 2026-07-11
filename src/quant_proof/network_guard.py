from __future__ import annotations

import ipaddress
import http.client
import os
import socket
import ssl
import subprocess
import urllib.request
from urllib.parse import urlsplit, urlunsplit
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator


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


class DirectRouteError(RuntimeError):
    """Raised when a physical-interface route cannot be proven."""


@dataclass
class DirectSocketRoute:
    host: str
    resolved_ip: str
    port: int
    interface: str
    interface_index: int
    interface_ipv4: str
    dns_server: str
    route_interface: str
    connected_local: tuple[str, int] | None = None
    connected_peer: tuple[str, int] | None = None


@dataclass(frozen=True)
class DirectHttpResponse:
    url: str
    status: int
    reason: str
    headers: dict[str, str]
    body: bytes
    route: DirectSocketRoute


_ORIGINAL_URLLIB_GETPROXIES = urllib.request.getproxies
_ORIGINAL_URLLIB_GETPROXIES_ENVIRONMENT = urllib.request.getproxies_environment
_ORIGINAL_SOCKET_TIMEOUT = socket.getdefaulttimeout()
_DIRECT_PATCH_ACTIVE = False
_IP_BOUND_IF = getattr(socket, "IP_BOUND_IF", 25)
_FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")
_TUNNEL_INTERFACE_PREFIXES = ("utun", "ppp", "ipsec")


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


def _run_text(command: list[str], timeout: float = 5.0) -> str:
    try:
        result = subprocess.run(command, text=True, capture_output=True, check=False, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DirectRouteError(f"could not run {' '.join(command)}: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit={result.returncode}"
        raise DirectRouteError(f"command failed ({' '.join(command)}): {detail}")
    return result.stdout.strip()


def physical_dns_server(interface: str) -> str:
    output = _run_text(["ipconfig", "getoption", interface, "domain_name_server"])
    candidates = [token.strip("{},") for token in output.replace("\n", " ").split()]
    for candidate in candidates:
        try:
            ipaddress.ip_address(candidate)
        except ValueError:
            continue
        return candidate
    raise DirectRouteError(f"no DHCP DNS server found for physical interface {interface}")


def physical_interface_ipv4(interface: str) -> str:
    output = _run_text(["ipconfig", "getifaddr", interface])
    try:
        address = ipaddress.ip_address(output.strip())
    except ValueError as exc:
        raise DirectRouteError(f"physical interface {interface} returned invalid IPv4 address {output!r}") from exc
    if address.version != 4:
        raise DirectRouteError(f"physical interface {interface} has no IPv4 address")
    return str(address)


def resolve_ipv4_via_dns(host: str, dns_server: str) -> str:
    output = _run_text(["dig", "+short", f"@{dns_server}", host, "A"])
    for candidate in output.splitlines():
        candidate = candidate.strip().rstrip(".")
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if address.version != 4:
            continue
        if address in _FAKE_IP_NETWORK:
            raise DirectRouteError(f"physical DNS returned fake-IP address {candidate} for {host}")
        if address.is_loopback or address.is_multicast or address.is_unspecified or address.is_reserved:
            raise DirectRouteError(f"physical DNS returned unusable address {candidate} for {host}")
        return candidate
    raise DirectRouteError(f"physical DNS {dns_server} returned no usable IPv4 address for {host}")


def route_interface_for_ip(ip: str) -> str:
    output = _run_text(["route", "-n", "get", ip])
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if line.startswith("interface:"):
            return line.split(":", 1)[1].strip()
    raise DirectRouteError(f"route lookup returned no interface for {ip}")


def require_non_tunnel_host_route(host: str) -> tuple[str, str]:
    try:
        addresses = socket.getaddrinfo(host, None, family=socket.AF_INET, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise DirectRouteError(f"system DNS failed for {host}: {exc}") from exc
    if not addresses:
        raise DirectRouteError(f"system DNS returned no IPv4 address for {host}")
    ip = str(addresses[0][4][0])
    address = ipaddress.ip_address(ip)
    interface = route_interface_for_ip(ip)
    if address in _FAKE_IP_NETWORK or interface.startswith(_TUNNEL_INTERFACE_PREFIXES):
        raise DirectRouteError(
            f"HTTP data source {host} resolves to {ip} via {interface}; no physical socket binding is available"
        )
    return ip, interface


def prepare_physical_socket_route(
    host: str,
    port: int,
    interface: str = "en0",
    dns_server: str | None = None,
) -> DirectSocketRoute:
    selected_dns = dns_server or physical_dns_server(interface)
    resolved_ip = resolve_ipv4_via_dns(host, selected_dns)
    route_interface = route_interface_for_ip(resolved_ip)
    try:
        interface_index = socket.if_nametoindex(interface)
    except OSError as exc:
        raise DirectRouteError(f"physical interface {interface} is unavailable: {exc}") from exc
    return DirectSocketRoute(
        host=host,
        resolved_ip=resolved_ip,
        port=int(port),
        interface=interface,
        interface_index=interface_index,
        interface_ipv4=physical_interface_ipv4(interface),
        dns_server=selected_dns,
        route_interface=route_interface,
    )


def direct_socket_route_message(route: DirectSocketRoute) -> str:
    message = (
        f"physical route active: {route.host}:{route.port} -> {route.resolved_ip} "
        f"forced via {route.interface}/{route.interface_ipv4} (dns={route.dns_server}, ifindex={route.interface_index}, "
        f"unbound_route={route.route_interface})"
    )
    if route.connected_local and route.connected_peer:
        message += f"; connected local={route.connected_local[0]}:{route.connected_local[1]} peer={route.connected_peer[0]}:{route.connected_peer[1]}"
    return message


def physical_http_get(
    url: str,
    interface: str = "en0",
    dns_server: str | None = None,
    timeout_seconds: float = 30.0,
    headers: dict[str, str] | None = None,
    max_bytes: int | None = None,
) -> DirectHttpResponse:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"physical HTTP URL must be absolute http/https: {url}")
    if parsed.username or parsed.password:
        raise ValueError("credentials are not allowed in physical HTTP URLs")
    if max_bytes is not None and max_bytes <= 0:
        raise ValueError("max_bytes must be positive")

    port = int(parsed.port or (443 if parsed.scheme == "https" else 80))
    route = prepare_physical_socket_route(
        host=parsed.hostname,
        port=port,
        interface=interface,
        dns_server=dns_server,
    )
    direct_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    connection: http.client.HTTPConnection | None = None
    try:
        direct_socket.setsockopt(socket.IPPROTO_IP, _IP_BOUND_IF, route.interface_index)
        direct_socket.settimeout(float(timeout_seconds))
        direct_socket.connect((route.resolved_ip, route.port))
        route.connected_local = direct_socket.getsockname()
        route.connected_peer = direct_socket.getpeername()
        if route.connected_local[0] != route.interface_ipv4:
            raise DirectRouteError(
                f"bound HTTP socket used local {route.connected_local[0]}, expected "
                f"{route.interface_ipv4} on {route.interface}"
            )
        if parsed.scheme == "https":
            direct_socket = ssl.create_default_context().wrap_socket(
                direct_socket,
                server_hostname=parsed.hostname,
            )

        connection = http.client.HTTPConnection(parsed.hostname, port=port, timeout=float(timeout_seconds))
        connection.sock = direct_socket
        target = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        request_headers = {
            "User-Agent": "quant-proof/1.0 direct-physical-route",
            "Accept": "*/*",
            "Accept-Encoding": "identity",
            "Connection": "close",
        }
        request_headers.update(headers or {})
        connection.request("GET", target, headers=request_headers)
        response = connection.getresponse()
        body = response.read(None if max_bytes is None else max_bytes + 1)
        if max_bytes is not None and len(body) > max_bytes:
            raise DirectRouteError(f"HTTP response exceeded max_bytes={max_bytes}: {url}")
        return DirectHttpResponse(
            url=url,
            status=int(response.status),
            reason=str(response.reason),
            headers={str(key).lower(): str(value) for key, value in response.getheaders()},
            body=body,
            route=route,
        )
    finally:
        if connection is not None:
            connection.close()
        else:
            direct_socket.close()


@contextmanager
def baostock_physical_route(
    interface: str = "en0",
    dns_server: str | None = None,
    host: str = "public-api.baostock.com",
    port: int = 10030,
) -> Iterator[DirectSocketRoute]:
    route = prepare_physical_socket_route(host=host, port=port, interface=interface, dns_server=dns_server)

    import baostock.common.context as baostock_context
    import baostock.util.socketutil as baostock_socketutil

    original_connect = baostock_socketutil.SocketUtil.connect

    def connect_on_physical_interface(_self) -> None:
        direct_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            direct_socket.setsockopt(socket.IPPROTO_IP, _IP_BOUND_IF, route.interface_index)
            direct_socket.connect((route.resolved_ip, route.port))
            route.connected_local = direct_socket.getsockname()
            route.connected_peer = direct_socket.getpeername()
            if route.connected_local[0] != route.interface_ipv4:
                raise DirectRouteError(
                    f"bound BaoStock socket used local {route.connected_local[0]}, expected {route.interface_ipv4} on {route.interface}"
                )
        except Exception:
            direct_socket.close()
            raise
        setattr(baostock_context, "default_socket", direct_socket)

    baostock_socketutil.SocketUtil.connect = connect_on_physical_interface
    try:
        yield route
    finally:
        baostock_socketutil.SocketUtil.connect = original_connect
