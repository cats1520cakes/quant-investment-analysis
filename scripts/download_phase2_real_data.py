from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

import pandas as pd
import yaml
from tenacity import retry, stop_after_attempt, wait_exponential
from tqdm import tqdm


@dataclass(frozen=True)
class Phase2Config:
    raw: dict
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
    def token_env(self) -> str:
        return str(self.raw.get("token_env", "TUSHARE_TOKEN"))

    @property
    def write_format(self) -> str:
        return str(self.raw.get("write_format", "parquet"))


def load_config(path: str | Path) -> Phase2Config:
    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    return Phase2Config(raw=raw, path=config_path)


def ensure_dirs(config: Phase2Config) -> None:
    for rel in [
        "00_meta/manifests",
        "00_meta/errors",
        "00_meta/logs",
        "raw/tushare/trade_cal",
        "raw/tushare/stock_basic",
        "raw/tushare/daily",
        "raw/tushare/adj_factor",
        "raw/tushare/daily_basic",
        "raw/tushare/stk_limit",
        "raw/tushare/suspend_d",
        "raw/tushare/namechange",
        "raw/tushare/index_daily",
        "raw/tushare/index_weight",
        "raw/tushare/fund_basic",
        "raw/tushare/fund_daily",
        "raw/tushare/fut_basic",
        "raw/tushare/fut_daily",
        "raw/tushare/opt_basic",
        "raw/tushare/opt_daily",
    ]:
        (config.data_root / rel).mkdir(parents=True, exist_ok=True)


def date_fmt(value: str) -> str:
    return f"{value[:4]}-{value[4:6]}-{value[6:8]}"


def normalize_filename(value: str) -> str:
    return value.replace(".", "_").replace("/", "_")


def write_frame(config: Phase2Config, table: str, name: str, frame: pd.DataFrame) -> Path:
    out_dir = config.data_root / "raw" / "tushare" / table
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = "parquet" if config.write_format == "parquet" else "csv"
    path = out_dir / f"{normalize_filename(name)}.{suffix}"
    if config.write_format == "parquet":
        frame.to_parquet(path, index=False)
    else:
        frame.to_csv(path, index=False, encoding="utf-8")
    return path


def checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def append_error(config: Phase2Config, table: str, message: str) -> None:
    path = config.data_root / "00_meta" / "errors" / f"{table}.log"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{datetime.now().isoformat()} {message}\n")


class TushareDownloader:
    def __init__(self, config: Phase2Config):
        import tushare as ts

        token = os.environ.get(config.token_env)
        if not token:
            raise RuntimeError(f"Missing {config.token_env}; export it before downloading Phase 2 real data.")
        self.config = config
        self.pro = ts.pro_api(token)
        self.sleep_seconds = float(config.raw.get("request_sleep_seconds", 0.25))
        self.retry_attempts = int(config.raw.get("retry_attempts", 3))

    def _sleep(self) -> None:
        if self.sleep_seconds > 0:
            time.sleep(self.sleep_seconds)

    def call(self, api_name: str, **kwargs) -> pd.DataFrame:
        @retry(
            stop=stop_after_attempt(self.retry_attempts),
            wait=wait_exponential(multiplier=1, min=1, max=8),
            reraise=True,
        )
        def _inner() -> pd.DataFrame:
            method = getattr(self.pro, api_name)
            frame = method(**kwargs)
            self._sleep()
            return frame if frame is not None else pd.DataFrame()

        return _inner()


def load_or_fetch_stock_basic(config: Phase2Config, dl: TushareDownloader, force: bool) -> pd.DataFrame:
    cache_path = config.data_root / "raw" / "tushare" / "stock_basic" / "all_status.parquet"
    if cache_path.exists() and not force:
        return pd.read_parquet(cache_path)
    frames = []
    fields = "ts_code,symbol,name,area,industry,market,list_date,delist_date,list_status,exchange,is_hs"
    for status in config.raw["tushare"].get("stock_statuses", ["L", "D", "P"]):
        frame = dl.call("stock_basic", exchange="", list_status=status, fields=fields)
        frame["queried_list_status"] = status
        frames.append(frame)
    out = pd.concat(frames, ignore_index=True).drop_duplicates("ts_code")
    write_frame(config, "stock_basic", "all_status", out)
    return out


def download_single_table(config: Phase2Config, dl: TushareDownloader, table: str, force: bool) -> list[dict]:
    records = []
    if table == "trade_cal":
        frame = dl.call("trade_cal", exchange="", start_date=config.start_date, end_date=config.end_date)
        path = write_frame(config, table, "all", frame)
        records.append(manifest_record(config, table, "all", path, frame))
    elif table == "stock_basic":
        frame = load_or_fetch_stock_basic(config, dl, force=force)
        path = config.data_root / "raw" / "tushare" / "stock_basic" / f"all_status.{config.write_format}"
        records.append(manifest_record(config, table, "all_status", path, frame))
    elif table == "namechange":
        frame = dl.call("namechange", start_date=config.start_date, end_date=config.end_date)
        path = write_frame(config, table, "all", frame)
        records.append(manifest_record(config, table, "all", path, frame))
    elif table == "suspend_d":
        frame = dl.call("suspend_d", suspend_date="", resume_date="", start_date=config.start_date, end_date=config.end_date)
        path = write_frame(config, table, "all", frame)
        records.append(manifest_record(config, table, "all", path, frame))
    elif table == "fund_basic":
        frames = []
        for market in config.raw["tushare"].get("etf_markets", ["E"]):
            frame = dl.call("fund_basic", market=market)
            frame["queried_market"] = market
            frames.append(frame)
        out = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        path = write_frame(config, table, "all", out)
        records.append(manifest_record(config, table, "all", path, out))
    elif table == "fut_basic":
        frame = dl.call("fut_basic", exchange=config.raw["tushare"]["exchanges"].get("futures", "CFFEX"))
        path = write_frame(config, table, "cffex", frame)
        records.append(manifest_record(config, table, "cffex", path, frame))
    elif table == "opt_basic":
        frames = []
        for exchange in config.raw["tushare"]["exchanges"].get("options", ["SSE", "SZSE", "CFFEX"]):
            try:
                frame = dl.call("opt_basic", exchange=exchange)
                frame["queried_exchange"] = exchange
                frames.append(frame)
            except Exception as exc:  # noqa: BLE001
                append_error(config, table, f"{exchange}: {type(exc).__name__}: {exc}")
        out = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        path = write_frame(config, table, "all", out)
        records.append(manifest_record(config, table, "all", path, out))
    else:
        raise ValueError(f"unsupported single table: {table}")
    return records


def manifest_record(config: Phase2Config, table: str, name: str, path: Path, frame: pd.DataFrame) -> dict:
    date_columns = ["trade_date", "cal_date", "suspend_date", "start_date", "list_date"]
    date_min = ""
    date_max = ""
    for column in date_columns:
        if column in frame.columns and not frame[column].dropna().empty:
            values = frame[column].dropna().astype(str)
            date_min = str(values.min())
            date_max = str(values.max())
            break
    key_column = "ts_code" if "ts_code" in frame.columns else "symbol" if "symbol" in frame.columns else ""
    key_values = frame[key_column].dropna().astype(str) if key_column else pd.Series(dtype=str)
    return {
        "source": "tushare",
        "table": table,
        "api_name": table,
        "name": name,
        "path": str(path),
        "file_format": path.suffix.lstrip("."),
        "rows": int(len(frame)),
        "empty": bool(frame.empty),
        "columns": ",".join(map(str, frame.columns)),
        "date_min": date_min,
        "date_max": date_max,
        "key_column": key_column,
        "key_min": str(key_values.min()) if not key_values.empty else "",
        "key_max": str(key_values.max()) if not key_values.empty else "",
        "start_date": config.start_date,
        "end_date": config.end_date,
        "sha256": checksum(path) if path.exists() else "",
        "downloaded_at": datetime.now().isoformat(timespec="seconds"),
    }


def iter_codes(stock_basic: pd.DataFrame, max_codes: int | None) -> list[str]:
    codes = sorted(stock_basic["ts_code"].dropna().unique().tolist())
    return codes[:max_codes] if max_codes else codes


def download_by_stock_code(config: Phase2Config, dl: TushareDownloader, table: str, codes: Iterable[str], force: bool) -> list[dict]:
    records = []
    for code in tqdm(list(codes), desc=table):
        out_path = config.data_root / "raw" / "tushare" / table / f"{normalize_filename(code)}.{config.write_format}"
        if out_path.exists() and not force:
            try:
                frame = pd.read_parquet(out_path) if config.write_format == "parquet" else pd.read_csv(out_path)
                records.append(manifest_record(config, table, code, out_path, frame))
                continue
            except Exception:
                pass
        try:
            frame = dl.call(table, ts_code=code, start_date=config.start_date, end_date=config.end_date)
            path = write_frame(config, table, code, frame)
            records.append(manifest_record(config, table, code, path, frame))
        except Exception as exc:  # noqa: BLE001
            append_error(config, table, f"{code}: {type(exc).__name__}: {exc}")
    return records


def download_stk_limit_by_date(config: Phase2Config, dl: TushareDownloader, force: bool, max_dates: int | None) -> list[dict]:
    trade_cal = dl.call("trade_cal", exchange="", start_date=config.start_date, end_date=config.end_date)
    dates = trade_cal.loc[trade_cal["is_open"] == 1, "cal_date"].astype(str).tolist()
    if max_dates:
        dates = dates[:max_dates]
    records = []
    for trade_date in tqdm(dates, desc="stk_limit"):
        out_path = config.data_root / "raw" / "tushare" / "stk_limit" / f"{trade_date}.{config.write_format}"
        if out_path.exists() and not force:
            frame = pd.read_parquet(out_path) if config.write_format == "parquet" else pd.read_csv(out_path)
            records.append(manifest_record(config, "stk_limit", trade_date, out_path, frame))
            continue
        try:
            frame = dl.call("stk_limit", trade_date=trade_date)
            path = write_frame(config, "stk_limit", trade_date, frame)
            records.append(manifest_record(config, "stk_limit", trade_date, path, frame))
        except Exception as exc:  # noqa: BLE001
            append_error(config, "stk_limit", f"{trade_date}: {type(exc).__name__}: {exc}")
    return records


def download_by_ts_codes(
    config: Phase2Config,
    dl: TushareDownloader,
    table: str,
    codes: Iterable[str],
    force: bool,
    api_kwargs_builder: Callable[[str], dict] | None = None,
) -> list[dict]:
    records = []
    for code in tqdm(list(codes), desc=table):
        out_path = config.data_root / "raw" / "tushare" / table / f"{normalize_filename(code)}.{config.write_format}"
        if out_path.exists() and not force:
            frame = pd.read_parquet(out_path) if config.write_format == "parquet" else pd.read_csv(out_path)
            records.append(manifest_record(config, table, code, out_path, frame))
            continue
        try:
            kwargs = api_kwargs_builder(code) if api_kwargs_builder else {"ts_code": code}
            frame = dl.call(table, **kwargs)
            path = write_frame(config, table, code, frame)
            records.append(manifest_record(config, table, code, path, frame))
        except Exception as exc:  # noqa: BLE001
            append_error(config, table, f"{code}: {type(exc).__name__}: {exc}")
    return records


def write_manifest(config: Phase2Config, records: list[dict]) -> Path:
    manifest_path = config.data_root / config.raw["paths"]["manifest"]
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(records)
    if manifest_path.exists():
        old = pd.read_csv(manifest_path)
        frame = pd.concat([old, frame], ignore_index=True)
        frame = frame.drop_duplicates(["source", "table", "name", "path"], keep="last")
    frame.to_csv(manifest_path, index=False, encoding="utf-8")
    return manifest_path


def parse_tables(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/phase2_real_data.yaml")
    parser.add_argument("--tables", default="", help="Comma-separated table list. Empty means configured core tables.")
    parser.add_argument("--max-codes", type=int, default=0)
    parser.add_argument("--max-dates", type=int, default=0)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    config = load_config(args.config)
    ensure_dirs(config)
    try:
        dl = TushareDownloader(config)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        print(f"Set it with: export {config.token_env}=\"...\"", file=sys.stderr)
        raise SystemExit(2) from exc

    requested = parse_tables(args.tables)
    if not requested:
        requested = [
            "trade_cal",
            "stock_basic",
            "daily",
            "adj_factor",
            "daily_basic",
            "stk_limit",
            "suspend_d",
            "namechange",
            "index_daily",
            "index_weight",
            "fund_basic",
            "fund_daily",
            "fut_basic",
            "fut_daily",
            "opt_basic",
            "opt_daily",
        ]

    records: list[dict] = []
    stock_basic = None
    if any(table in requested for table in ["daily", "adj_factor", "daily_basic", "stock_basic"]):
        stock_basic = load_or_fetch_stock_basic(config, dl, force=args.force)
        if "stock_basic" in requested:
            path = config.data_root / "raw" / "tushare" / "stock_basic" / f"all_status.{config.write_format}"
            records.append(manifest_record(config, "stock_basic", "all_status", path, stock_basic))

    for table in requested:
        if table == "stock_basic":
            continue
        try:
            if table in {"trade_cal", "namechange", "suspend_d", "fund_basic", "fut_basic", "opt_basic"}:
                records.extend(download_single_table(config, dl, table, force=args.force))
            elif table in {"daily", "adj_factor", "daily_basic"}:
                assert stock_basic is not None
                records.extend(download_by_stock_code(config, dl, table, iter_codes(stock_basic, args.max_codes or None), force=args.force))
            elif table == "stk_limit":
                records.extend(download_stk_limit_by_date(config, dl, force=args.force, max_dates=args.max_dates or None))
            elif table == "index_daily":
                records.extend(
                    download_by_ts_codes(
                        config,
                        dl,
                        table,
                        config.raw["tushare"]["exchanges"]["index"],
                        force=args.force,
                        api_kwargs_builder=lambda code: {"ts_code": code, "start_date": config.start_date, "end_date": config.end_date},
                    )
                )
            elif table == "index_weight":
                records.extend(
                    download_by_ts_codes(
                        config,
                        dl,
                        table,
                        config.raw["tushare"]["exchanges"]["index"],
                        force=args.force,
                        api_kwargs_builder=lambda code: {"index_code": code, "start_date": config.start_date, "end_date": config.end_date},
                    )
                )
            elif table == "fund_daily":
                fund_basic_path = config.data_root / "raw" / "tushare" / "fund_basic" / f"all.{config.write_format}"
                if not fund_basic_path.exists():
                    records.extend(download_single_table(config, dl, "fund_basic", force=args.force))
                fund_basic = pd.read_parquet(fund_basic_path) if config.write_format == "parquet" else pd.read_csv(fund_basic_path)
                codes = fund_basic["ts_code"].dropna().unique().tolist()
                records.extend(
                    download_by_ts_codes(
                        config,
                        dl,
                        table,
                        codes[: args.max_codes] if args.max_codes else codes,
                        force=args.force,
                        api_kwargs_builder=lambda code: {"ts_code": code, "start_date": config.start_date, "end_date": config.end_date},
                    )
                )
            elif table == "fut_daily":
                fut_basic_path = config.data_root / "raw" / "tushare" / "fut_basic" / f"cffex.{config.write_format}"
                if not fut_basic_path.exists():
                    records.extend(download_single_table(config, dl, "fut_basic", force=args.force))
                fut_basic = pd.read_parquet(fut_basic_path) if config.write_format == "parquet" else pd.read_csv(fut_basic_path)
                codes = fut_basic["ts_code"].dropna().unique().tolist()
                records.extend(
                    download_by_ts_codes(
                        config,
                        dl,
                        table,
                        codes[: args.max_codes] if args.max_codes else codes,
                        force=args.force,
                        api_kwargs_builder=lambda code: {"ts_code": code, "start_date": config.start_date, "end_date": config.end_date},
                    )
                )
            elif table == "opt_daily":
                opt_basic_path = config.data_root / "raw" / "tushare" / "opt_basic" / f"all.{config.write_format}"
                if not opt_basic_path.exists():
                    records.extend(download_single_table(config, dl, "opt_basic", force=args.force))
                opt_basic = pd.read_parquet(opt_basic_path) if config.write_format == "parquet" else pd.read_csv(opt_basic_path)
                codes = opt_basic["ts_code"].dropna().unique().tolist()
                records.extend(
                    download_by_ts_codes(
                        config,
                        dl,
                        table,
                        codes[: args.max_codes] if args.max_codes else codes,
                        force=args.force,
                        api_kwargs_builder=lambda code: {"ts_code": code, "start_date": config.start_date, "end_date": config.end_date},
                    )
                )
            else:
                append_error(config, table, "unsupported table requested")
        except Exception as exc:  # noqa: BLE001
            append_error(config, table, f"table failed: {type(exc).__name__}: {exc}")

    manifest = write_manifest(config, records)
    print(f"manifest={manifest}")
    print(f"records={len(records)}")


if __name__ == "__main__":
    main()
