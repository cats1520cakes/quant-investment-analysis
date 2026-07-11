from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from quant_proof.free_sources.baostock_adapter import load_config
from quant_proof.free_sources.download_planner import write_download_plan


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a resumable BaoStock download shard plan from a frozen universe.")
    parser.add_argument("--config", default="config/phase2_free_real_data.yaml")
    parser.add_argument("--universe-file", default="")
    parser.add_argument("--shard-size", type=int, default=0)
    args = parser.parse_args()

    config = load_config(args.config)
    download_config = config.raw.get("download", {})
    scope = str(download_config.get("universe_scope", "point_in_time"))
    universe_file = Path(args.universe_file) if args.universe_file else (
        config.data_root
        / "00_meta"
        / "universes"
        / f"phase2_free_{scope}_{config.start_date}_{config.end_date}.csv"
    )
    shard_size = args.shard_size or int(download_config.get("shard_size", 50))
    manifest_path, missing_path, manifest = write_download_plan(config.data_root, universe_file, shard_size=shard_size)
    print(f"universe={universe_file}")
    print(f"missing_codes={int(manifest['codes'].sum()) if not manifest.empty else 0}")
    print(f"shards={len(manifest)}")
    print(f"plan={manifest_path}")
    print(f"missing={missing_path}")
    if not manifest.empty:
        print(f"first_codes_file={manifest.iloc[0]['codes_file']}")


if __name__ == "__main__":
    main()
