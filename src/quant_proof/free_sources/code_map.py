from __future__ import annotations


def baostock_to_ts_code(code: str) -> str:
    text = str(code).strip().lower()
    if "." not in text:
        raise ValueError(f"invalid BaoStock code: {code}")
    exchange, symbol = text.split(".", 1)
    suffix = {"sh": "SH", "sz": "SZ", "bj": "BJ"}.get(exchange)
    if suffix is None:
        raise ValueError(f"unsupported BaoStock exchange prefix: {exchange}")
    return f"{symbol.upper()}.{suffix}"


def ts_code_to_baostock(ts_code: str) -> str:
    text = str(ts_code).strip().upper()
    if "." not in text:
        raise ValueError(f"invalid ts_code: {ts_code}")
    symbol, suffix = text.split(".", 1)
    prefix = {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(suffix)
    if prefix is None:
        raise ValueError(f"unsupported ts_code exchange suffix: {suffix}")
    return f"{prefix}.{symbol.lower()}"


def infer_ts_code_from_numeric(symbol: str) -> str:
    text = str(symbol).strip()
    if text.startswith(("60", "68", "90")):
        return f"{text}.SH"
    if text.startswith(("00", "30", "20")):
        return f"{text}.SZ"
    if text.startswith(("43", "83", "87", "92")):
        return f"{text}.BJ"
    raise ValueError(f"cannot infer exchange from symbol: {symbol}")
