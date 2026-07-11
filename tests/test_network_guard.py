from __future__ import annotations

import baostock.common.context as baostock_context
import baostock.util.socketutil as baostock_socketutil

from quant_proof import network_guard


def test_prepare_physical_socket_route_records_unbound_tunnel_route(monkeypatch) -> None:
    monkeypatch.setattr(network_guard, "physical_dns_server", lambda interface: "192.168.3.1")
    monkeypatch.setattr(network_guard, "resolve_ipv4_via_dns", lambda host, dns: "114.94.20.42")
    monkeypatch.setattr(network_guard, "route_interface_for_ip", lambda ip: "utun5")
    monkeypatch.setattr(network_guard.socket, "if_nametoindex", lambda interface: 9)
    monkeypatch.setattr(network_guard, "physical_interface_ipv4", lambda interface: "192.168.3.36")

    route = network_guard.prepare_physical_socket_route("public-api.baostock.com", 10030)

    assert route.resolved_ip == "114.94.20.42"
    assert route.interface == "en0"
    assert route.interface_index == 9
    assert route.interface_ipv4 == "192.168.3.36"
    assert route.route_interface == "utun5"


def test_baostock_route_binds_only_its_login_socket(monkeypatch) -> None:
    route = network_guard.DirectSocketRoute(
        host="public-api.baostock.com",
        resolved_ip="114.94.20.42",
        port=10030,
        interface="en0",
        interface_index=9,
        interface_ipv4="192.168.3.36",
        dns_server="192.168.3.1",
        route_interface="en0",
    )
    calls: list[tuple] = []

    class FakeSocket:
        def setsockopt(self, level, option, value) -> None:
            calls.append(("setsockopt", level, option, value))

        def connect(self, address) -> None:
            calls.append(("connect", address))

        def getsockname(self):
            return ("192.168.3.36", 54321)

        def getpeername(self):
            return ("114.94.20.42", 10030)

        def close(self) -> None:
            calls.append(("close",))

    original_connect = baostock_socketutil.SocketUtil.connect
    monkeypatch.setattr(network_guard, "prepare_physical_socket_route", lambda **kwargs: route)
    monkeypatch.setattr(network_guard.socket, "socket", lambda *args: FakeSocket())

    with network_guard.baostock_physical_route() as active_route:
        assert active_route == route
        baostock_socketutil.SocketUtil().connect()
        assert baostock_socketutil.SocketUtil.connect is not original_connect

    assert baostock_socketutil.SocketUtil.connect is original_connect
    assert calls[0] == ("setsockopt", network_guard.socket.IPPROTO_IP, network_guard._IP_BOUND_IF, 9)
    assert calls[1] == ("connect", ("114.94.20.42", 10030))
    assert route.connected_local == ("192.168.3.36", 54321)
    assert route.connected_peer == ("114.94.20.42", 10030)
    assert isinstance(getattr(baostock_context, "default_socket"), FakeSocket)
    delattr(baostock_context, "default_socket")


def test_generic_http_route_rejects_fake_ip_tunnel(monkeypatch) -> None:
    monkeypatch.setattr(
        network_guard.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(network_guard.socket.AF_INET, network_guard.socket.SOCK_STREAM, 6, "", ("198.18.0.70", 0))],
    )
    monkeypatch.setattr(network_guard, "route_interface_for_ip", lambda ip: "utun5")

    try:
        network_guard.require_non_tunnel_host_route("api.tushare.pro")
    except network_guard.DirectRouteError as exc:
        assert "utun5" in str(exc)
    else:
        raise AssertionError("tunnel route should have been rejected")


def test_physical_http_get_binds_socket_and_records_peer(monkeypatch) -> None:
    route = network_guard.DirectSocketRoute(
        host="www.cffex.com.cn",
        resolved_ip="58.32.205.2",
        port=80,
        interface="en0",
        interface_index=12,
        interface_ipv4="192.168.3.36",
        dns_server="192.168.3.1",
        route_interface="utun5",
    )
    calls: list[tuple] = []

    class FakeSocket:
        def setsockopt(self, level, option, value) -> None:
            calls.append(("setsockopt", level, option, value))

        def settimeout(self, value) -> None:
            calls.append(("settimeout", value))

        def connect(self, address) -> None:
            calls.append(("connect", address))

        def getsockname(self):
            return ("192.168.3.36", 55001)

        def getpeername(self):
            return ("58.32.205.2", 80)

        def close(self) -> None:
            calls.append(("socket_close",))

    class FakeResponse:
        status = 200
        reason = "OK"

        def read(self, size=None):
            calls.append(("read", size))
            return b"zip-bytes"

        def getheaders(self):
            return [("Content-Type", "application/zip")]

    class FakeConnection:
        def __init__(self, host, port, timeout) -> None:
            calls.append(("connection", host, port, timeout))
            self.sock = None

        def request(self, method, target, headers) -> None:
            calls.append(("request", method, target, headers["Accept-Encoding"]))

        def getresponse(self):
            return FakeResponse()

        def close(self) -> None:
            calls.append(("connection_close",))
            self.sock.close()

    monkeypatch.setattr(network_guard, "prepare_physical_socket_route", lambda **kwargs: route)
    monkeypatch.setattr(network_guard.socket, "socket", lambda *args: FakeSocket())
    monkeypatch.setattr(network_guard.http.client, "HTTPConnection", FakeConnection)

    response = network_guard.physical_http_get(
        "http://www.cffex.com.cn/sj/historysj/202401/zip/202401.zip",
        max_bytes=1024,
    )

    assert response.status == 200
    assert response.body == b"zip-bytes"
    assert response.headers == {"content-type": "application/zip"}
    assert response.route.connected_local == ("192.168.3.36", 55001)
    assert response.route.connected_peer == ("58.32.205.2", 80)
    assert calls[0] == ("setsockopt", network_guard.socket.IPPROTO_IP, network_guard._IP_BOUND_IF, 12)
    assert ("connect", ("58.32.205.2", 80)) in calls
    assert ("request", "GET", "/sj/historysj/202401/zip/202401.zip", "identity") in calls
