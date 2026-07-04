from __future__ import annotations

import argparse
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from quant_proof.realdata.free_panel_builder import FreePanelBuildError, build_and_write_free_stock_panel


def main() -> None:
    parser = argparse.ArgumentParser(description="Build Phase 2 free-real stock panel.")
    parser.add_argument("--config", default="config/phase2_free_real_data.yaml")
    args = parser.parse_args()

    try:
        path = build_and_write_free_stock_panel(args.config)
    except (FreePanelBuildError, ValueError, KeyError) as exc:
        print(f"phase2 free stock panel build failed: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    print(f"stock_panel={path}")


if __name__ == "__main__":
    main()
