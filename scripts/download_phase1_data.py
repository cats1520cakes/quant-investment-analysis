from __future__ import annotations

import argparse
from pathlib import Path

from quant_proof.config import ensure_data_dirs, load_config
from quant_proof.data import (
    build_close_matrix,
    download_etf_daily,
    load_raw_etf_prices,
    write_download_manifest,
    write_processed_prices,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/phase1.yaml")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--retries", type=int, default=3)
    args = parser.parse_args()

    config = load_config(args.config)
    ensure_data_dirs(config.data_root)
    outputs = download_etf_daily(config, force=args.force, retries=args.retries)
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
