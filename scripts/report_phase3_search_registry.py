from __future__ import annotations

import argparse
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from quant_proof.search_registry import (  # noqa: E402
    render_markdown_summary,
    validate_search_registry,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate the Phase 3 search registry and render a Markdown inventory."
    )
    parser.add_argument(
        "--registry",
        default="config/phase3_search_registry.yaml",
        help="Repo-relative or absolute registry path.",
    )
    parser.add_argument(
        "--repo-root",
        default=str(ROOT),
        help="Repository root used to resolve configs and completeness checks.",
    )
    parser.add_argument(
        "--output",
        default="-",
        help="Output Markdown path, or '-' for stdout (default).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo_root = Path(args.repo_root).resolve()
    registry_path = Path(args.registry)
    if not registry_path.is_absolute():
        registry_path = repo_root / registry_path
    registry = validate_search_registry(registry_path, repo_root=repo_root)
    report = render_markdown_summary(registry)

    if args.output == "-":
        sys.stdout.write(report)
        return 0
    output_path = Path(args.output)
    if not output_path.is_absolute():
        output_path = Path.cwd() / output_path
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
