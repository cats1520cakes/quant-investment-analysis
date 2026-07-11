from __future__ import annotations

import argparse
from contextlib import nullcontext
from pathlib import Path

from quant_proof.config import ensure_data_dirs, load_config
from quant_proof.data import (
    build_close_matrix,
    download_etf_daily,
    load_raw_etf_prices,
    write_download_manifest,
    write_processed_prices,
)
from quant_proof.network_guard import (
    DirectRouteError,
    ProxyDetectedError,
    baostock_physical_route,
    direct_network_message,
    direct_socket_route_message,
    require_direct_network,
    require_non_tunnel_host_route,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/phase1.yaml")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--network-interface", default="en0")
    parser.add_argument("--dns-server", default="")
    parser.add_argument("--allow-proxy", action="store_true", help="Allow visible proxy/VPN settings for this download.")
    args = parser.parse_args()

    config = load_config(args.config)
    ensure_data_dirs(config.data_root)
    try:
        visible = require_direct_network(allow_proxy=args.allow_proxy)
        if not args.allow_proxy:
            print(f"[network] {direct_network_message(visible)}", flush=True)
    except (ProxyDetectedError, DirectRouteError) as exc:
        print(str(exc))
        raise SystemExit(2) from exc
    source = str(config.raw.get("data_source", "eastmoney_etf"))
    if source != "baostock_index" and not args.allow_proxy:
        try:
            if visible:
                raise DirectRouteError(
                    "HTTP market-data download is blocked while macOS proxy settings are active; no target socket binding is available"
                )
            require_non_tunnel_host_route("push2his.eastmoney.com")
        except DirectRouteError as exc:
            print(str(exc))
            raise SystemExit(2) from exc
    route_context = (
        baostock_physical_route(interface=args.network_interface, dns_server=args.dns_server or None)
        if source == "baostock_index" and not args.allow_proxy
        else nullcontext(None)
    )
    try:
        with route_context as route:
            if route is not None:
                print(f"[network] {direct_socket_route_message(route)}", flush=True)
            outputs = download_etf_daily(config, force=args.force, retries=args.retries)
            if route is not None:
                print(f"[network] verified {direct_socket_route_message(route)}", flush=True)
    except DirectRouteError as exc:
        print(str(exc))
        raise SystemExit(2) from exc
    frames = load_raw_etf_prices(config)
    close, amount = build_close_matrix(frames)
    processed = write_processed_prices(config, close, amount)
    manifest = write_download_manifest(config, outputs, close)

    print(f"data_root={config.data_root}")
    print(f"downloaded_or_cached={len(outputs)}")
    print(f"close_shape={close.shape}")
    print(f"processed_close={processed['close']}")
    print(f"manifest={manifest}")


if __name__ == "__main__":
    main()
