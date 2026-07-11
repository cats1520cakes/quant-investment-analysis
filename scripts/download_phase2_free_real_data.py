from __future__ import annotations

import argparse
import sys
from contextlib import nullcontext
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from quant_proof.free_sources.baostock_adapter import UNIVERSE_SCOPES, download_baostock_free_real, load_config
from quant_proof.network_guard import (
    DirectRouteError,
    ProxyDetectedError,
    baostock_physical_route,
    direct_network_message,
    direct_socket_route_message,
    require_direct_network,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Download Phase 2 free-real BaoStock data.")
    parser.add_argument("--config", default="config/phase2_free_real_data.yaml")
    parser.add_argument("--max-codes", type=int, default=0)
    parser.add_argument("--start-index", type=int, default=0, help="0-based inclusive start index within the selected listed-stock universe.")
    parser.add_argument("--end-index", type=int, default=0, help="0-based exclusive end index within the selected listed-stock universe; 0 means no slice end.")
    parser.add_argument("--codes-file", default="", help="Optional newline-delimited BaoStock source codes to download instead of an index slice.")
    parser.add_argument("--universe-scope", choices=UNIVERSE_SCOPES, default=None)
    parser.add_argument("--refresh-metadata", action="store_true")
    parser.add_argument("--metadata-only", action="store_true")
    parser.add_argument("--manifest-batch-size", type=int, default=0)
    parser.add_argument("--network-interface", default="", help="Physical interface used for the BaoStock socket in direct mode.")
    parser.add_argument("--dns-server", default="", help="Physical-path DNS server; defaults to the interface DHCP DNS server.")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--allow-proxy", action="store_true", help="Allow visible proxy/VPN settings for this download.")
    args = parser.parse_args()

    config = load_config(args.config)
    if config.raw.get("data_tier") != "free_real":
        print("config data_tier must be free_real", file=sys.stderr)
        raise SystemExit(2)
    try:
        visible = require_direct_network(allow_proxy=args.allow_proxy)
        if not args.allow_proxy:
            print(f"[network] {direct_network_message(visible)}", flush=True)
    except (ProxyDetectedError, DirectRouteError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc
    codes_override = None
    if args.codes_file:
        codes_path = Path(args.codes_file)
        codes_override = [line.strip() for line in codes_path.read_text(encoding="utf-8").splitlines() if line.strip() and not line.startswith("#")]
    download_config = config.raw.get("download", {})
    universe_scope = args.universe_scope or str(download_config.get("universe_scope", "point_in_time"))
    network_interface = args.network_interface or str(download_config.get("physical_interface", "en0"))
    manifest_batch_size = args.manifest_batch_size or int(download_config.get("manifest_batch_size", 50))
    route_context = (
        nullcontext(None)
        if args.allow_proxy
        else baostock_physical_route(interface=network_interface, dns_server=args.dns_server or None)
    )
    try:
        with route_context as route:
            if route is not None:
                print(f"[network] {direct_socket_route_message(route)}", flush=True)
            manifest = download_baostock_free_real(
                config,
                max_codes=args.max_codes or None,
                force=args.force,
                start_index=args.start_index,
                end_index=args.end_index or None,
                codes_override=codes_override,
                universe_scope=universe_scope,
                refresh_metadata=args.refresh_metadata or bool(download_config.get("refresh_metadata", False)),
                metadata_only=args.metadata_only,
                manifest_batch_size=manifest_batch_size,
            )
            if route is not None:
                print(f"[network] verified {direct_socket_route_message(route)}", flush=True)
    except (ProxyDetectedError, DirectRouteError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc
    print(f"manifest={manifest}")


if __name__ == "__main__":
    main()
