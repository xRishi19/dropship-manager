#!/usr/bin/env python3
"""Download eBay data and generate the weekly sourcing schedule."""
import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from backend.services.sourcing.analysis import analyze
from backend.config import load_config
from backend.db.connection import connect
from backend.services.sync.ebay_sync import sync
from backend.db.ingest import import_file
from backend.services.sourcing.output import atomic_write, csv_text, export_analysis
from backend.services.sourcing.weekly import weekly


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--import', dest='import_data', action='store_true', help='Download current eBay data; --files enables historical backfill')
    mode.add_argument('--analyze', action='store_true', help='Analyze stored data without API calls')
    mode.add_argument('--weekly', action='store_true', help='Download eBay data, analyze and select 14 keywords')
    parser.add_argument('--files', nargs='+', type=Path, help='Optional historical CSV/XLSX files, with --import')
    parser.add_argument('--kind', choices=['auto', 'orders', 'listings'], default='auto')
    parser.add_argument('--snapshot-date', help='YYYY-MM-DD for listing file snapshots')
    parser.add_argument('--as-of', default=datetime.now(timezone.utc).date().isoformat())
    parser.add_argument('--offline', action='store_true', help='Use stored data for --weekly without eBay download')
    parser.add_argument('--refresh', action='store_true', help='Regenerate an already-saved week (calls OpenAI again)')
    parser.add_argument('--config', default='config.json')
    parser.add_argument('--env', default='.env')
    args = parser.parse_args(argv)
    if args.files and not args.import_data:
        parser.error('--files requires --import')
    if args.offline and not args.weekly:
        parser.error('--offline requires --weekly')
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    load_dotenv(args.env)
    db = None
    try:
        as_of = datetime.strptime(args.as_of, '%Y-%m-%d').date()
        today = datetime.now(timezone.utc).date()
        if as_of > today:
            raise ValueError('--as-of cannot be in the future')
        config = load_config(args.config)
        db = connect(config.database)
        if args.import_data and args.files:
            count = sum(import_file(db, p, args.kind, args.snapshot_date, config.column_mapping) for p in args.files)
            print(f'Imported {count} historical rows.')
            return 0
        if args.import_data or (args.weekly and not args.offline):
            orders, listings = sync(db, config)
            archive = Path(config.reports_dir) / today.isoformat()
            for name, rows, fields in [('orders', orders, ['order_id', 'line_id', 'item_id', 'title', 'sku', 'quantity', 'sold_date']),
                                       ('listings', listings, ['item_id', 'title', 'sku', 'sold_quantity', 'observed_date', 'status', 'start_date', 'end_date'])]:
                atomic_write(archive / f'{name}.csv', csv_text(rows, fields))
            print(f'Downloaded {len(orders)} order lines and {len(listings)} listings from eBay.')
        if args.analyze:
            diagnostics = {}
            candidates, listings = analyze(db, config, args.as_of, diagnostics)
            export_analysis(config.output_dir, candidates, listings, diagnostics)
            print(f'Analyzed {len(listings)} listings; {len(candidates)} qualified candidates in {config.output_dir}.')
        if args.weekly:
            result = weekly(db, config, args.as_of, refresh=args.refresh)
            print(f'Saved exactly {len(result["results"])} keywords for week {result["week"]} to {config.output_dir}.')
        return 0
    except Exception as exc:
        # Never print API bodies, request headers, access tokens or tracebacks.
        from openai import OpenAIError
        if isinstance(exc, OpenAIError):
            logging.error('OpenAI request failed (%s). Check OPENAI_API_KEY, model access and quota.', type(exc).__name__)
        else:
            logging.error('%s', exc)
        return 1
    finally:
        if db is not None:
            db.close()


if __name__ == '__main__':
    sys.exit(main())
