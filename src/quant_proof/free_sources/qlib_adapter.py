from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .validators import DataTier


@dataclass(frozen=True)
class QlibProxyDataset:
    path: Path
    data_tier: DataTier = DataTier.PROXY_RESEARCH


def describe_proxy_dataset(path: str | Path) -> QlibProxyDataset:
    return QlibProxyDataset(path=Path(path).expanduser())
