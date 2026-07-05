from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from quant_proof.free_sources.baostock_adapter import download_baostock_free_real, load_config
from quant_proof.network_guard import ProxyDetectedError, direct_network_message, require_direct_network


def main() -> None:
    parser = argparse.ArgumentParser(description="Download Phase 2 free-real BaoStock data.")
    parser.add_argument("--config", default="config/phase2_free_real_data.yaml")
    parser.add_argument("--max-codes", type=int, default=0)
    parser.add_argument("--start-index", type=int, default=0, help="0-based inclusive start index within the selected listed-stock universe.")
    parser.add_argument("--end-index", type=int, default=0, help="0-based exclusive end index within the selected listed-stock universe; 0 means no slice end.")
    parser.add_argument("--codes-file", default="", help="Optional newline-delimited BaoStock source codes to download instead of an index slice.")
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
    except ProxyDetectedError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc
    codes_override = None
    if args.codes_file:
        codes_path = Path(args.codes_file)
        codes_override = [line.strip() for line in codes_path.read_text(encoding="utf-8").splitlines() if line.strip() and not line.startswith("#")]
    manifest = download_baostock_free_real(
        config,
        max_codes=args.max_codes or None,
        force=args.force,
        start_index=args.start_index,
        end_index=args.end_index or None,
        codes_override=codes_override,
    )
    print(f"manifest={manifest}")


if __name__ == "__main__":
    main()
