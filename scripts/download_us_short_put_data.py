"""Download public daily prices, NOT historical option quotes. Preserve raw SHA256."""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sys
import time
import urllib.request
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]

def download(symbol: str, out: Path) -> dict:
    url = f'https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=10y&interval=1d&events=div%2Csplits'
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=45) as response:
        raw = response.read()
    (out / f'{symbol}.json').write_bytes(raw)
    result = json.loads(raw)['chart']['result'][0]
    dates = pd.to_datetime(result['timestamp'], unit='s', utc=True).tz_convert('America/New_York').date
    quotes = result['indicators']['quote'][0]
    frame = pd.DataFrame({'date': dates, 'close': quotes['close'], 'dividend': 0.0, 'split': 1.0})
    for event in result.get('events', {}).get('dividends', {}).values():
        day = pd.Timestamp(event['date'], unit='s', tz='UTC').tz_convert('America/New_York').date()
        frame.loc[frame.date == day, 'dividend'] = event['amount']
    for event in result.get('events', {}).get('splits', {}).values():
        day = pd.Timestamp(event['date'], unit='s', tz='UTC').tz_convert('America/New_York').date()
        frame.loc[frame.date == day, 'split'] = event['numerator'] / event['denominator']
    frame = frame.dropna(subset=['close']).sort_values('date')
    frame.to_csv(out / f'{symbol}.csv', index=False)
    return {'symbol': symbol, 'url': url, 'retrieved_utc': pd.Timestamp.now(tz='UTC').isoformat(),
            'raw_sha256': hashlib.sha256(raw).hexdigest(), 'csv_sha256': hashlib.sha256((out / f'{symbol}.csv').read_bytes()).hexdigest(),
            'rows': len(frame), 'first': str(frame.date.min()), 'last': str(frame.date.max()),
            'boundary': 'Historical underlying/ETF daily close only. Dividend timestamps are ex dates, not payable dates. No option quotes.'}

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, default=ROOT / 'data/us_short_put')
    p.add_argument('--symbols', nargs='+', default=['SGOV','SPY','QQQ','KWEB','ARKK','COIN'])
    args = p.parse_args(); args.output.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=3) as pool:
        rows = list(pool.map(lambda s: download(s, args.output), args.symbols))
    (args.output / 'manifest.json').write_text(json.dumps(rows, indent=2) + '\n')
    print(json.dumps(rows, indent=2))

if __name__ == '__main__': main()
