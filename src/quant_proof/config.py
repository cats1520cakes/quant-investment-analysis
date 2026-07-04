from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

import yaml


@dataclass(frozen=True)
class ProjectConfig:
    raw: Dict[str, Any]
    path: Path

    @property
    def data_root(self) -> Path:
        return Path(self.raw["data_root"]).expanduser()

    @property
    def start_date(self) -> str:
        return str(self.raw.get("start_date") or "20100101")

    @property
    def end_date(self) -> str:
        configured = self.raw.get("end_date")
        if configured:
            return str(configured)
        return datetime.now().strftime("%Y%m%d")

    @property
    def monthly_deposit(self) -> float:
        return float(self.raw.get("monthly_deposit", 30000))

    @property
    def target_month_12(self) -> float:
        return float(self.raw.get("target_month_12", 500000))

    @property
    def target_month_24(self) -> float:
        return float(self.raw.get("target_month_24", 1200000))

    @property
    def seed(self) -> int:
        return int(self.raw.get("random_seed", 20260704))


def load_config(path: str | Path) -> ProjectConfig:
    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    return ProjectConfig(raw=raw, path=config_path)


def ensure_data_dirs(data_root: Path) -> None:
    for name in ("raw/akshare/etf_daily", "raw/baostock/index_daily", "processed", "cache", "reports", "logs"):
        (data_root / name).mkdir(parents=True, exist_ok=True)
