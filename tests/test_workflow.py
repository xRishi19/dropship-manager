"""Exercise the actual CLI, persistence, analysis and exports with synthetic APIs."""
import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import main
from backend.services.sync.ebay_sync import sync
from backend.services.sourcing.selection import Selection

PHRASES = ['jeep wrangler', 'toyota tacoma', 'garage door', 'pressure washer',
           'replacement filter', 'garden hose', 'shower curtain', 'coffee maker',
           'vacuum cleaner', 'ceiling fan', 'bike rack', 'phone mount', 'dog leash', 'pool pump']


def test_weekly_command_downloads_analyzes_selects_and_exports(tmp_path, monkeypatch):
    """Regression check: weekly command downloads analyzes selects and exports.

    Args:
        tmp_path: Pytest temporary directory for isolated databases, configuration files,
            exports, and backups.
        monkeypatch: Pytest patch manager for temporary environment, clock, client, or
            dependency overrides.

    Returns:
        None. Successful completion means the assertions passed; unexpected results raise
        AssertionError or fail a pytest exception check.
    """
    day = datetime.now(timezone.utc).date().isoformat()
    orders, listings = [], []
    for index, phrase in enumerate(PHRASES):
        for variant, suffix in enumerate(['adapter', 'bracket', 'cable']):
            item = f'{index}-{variant}'
            title = f'{phrase} {suffix}'
            listings.append({'item_id': item, 'title': title, 'sku': item, 'sold_quantity': 999,
                             'observed_date': day, 'status': 'active'})
            if variant < 2:
                # The first seven phrases sell 18 units (Proven); the rest sell 2 (Explore).
                quantity = (10 if variant == 0 else 8) if index < 7 else 1
                orders.append({'orderId': 'order-' + item, 'creationDate': day + 'T12:00:00Z',
                               'lineItems': [{'lineItemId': item + '-line', 'legacyItemId': item, 'title': title,
                                              'sku': item, 'quantity': quantity}]})
    class API:
        def raw_orders(self, *args):
            """Provide synthetic eBay orders through the production-compatible download interface.

            Args:
                args: Positional arguments accepted for compatibility with the production callable being
                    mocked.

            Returns:
                List of synthetic Fulfillment orders; recording backfill fakes return an empty list.
            """
            return orders
        def listings(self, *args):
            """Provide synthetic listing snapshots through the production-compatible eBay download interface.

            Args:
                args: Positional arguments accepted for compatibility with the production callable being
                    mocked.

            Returns:
                List of synthetic listing dictionaries with sold counters and observation dates.
            """
            return listings
    monkeypatch.setattr(main, 'sync', lambda db, cfg: sync(db, cfg, client=API()))
    model_calls = []
    def parse(**kwargs):
        """Capture a mocked structured-model request and return a synthetic validated weekly selection response.

        Args:
            kwargs: Keyword arguments forwarded to the mocked callable or used to override synthetic
                payload fields.

        Returns:
            SimpleNamespace with status completed and a Selection instance in output_parsed.
        """
        model_calls.append(kwargs)
        data = json.loads(kwargs['input'][1]['content'])
        proven, explore = data['proven_candidates'], data['explore_candidates']
        assert {c['keyword'] for c in proven} == set(PHRASES[:7])
        assert {c['keyword'] for c in explore} == set(PHRASES[7:])
        assert {c['total_units_sold'] for c in proven} == {18}
        assert {c['unsold_listings'] for c in proven + explore} == {1}
        reason = 'Reusable product-title phrase with distributed unit sales.'
        result = {'proven': [{'keyword': c['keyword'], 'reason': reason, 'confidence': .85} for c in proven],
                  'explore': [{'keyword': c['keyword'], 'reason': reason, 'confidence': .6, 'market': 'new_category'}
                              for c in explore]}
        return SimpleNamespace(status='completed', output_parsed=Selection.model_validate(result))
    monkeypatch.setattr('openai.OpenAI', lambda **kwargs: SimpleNamespace(responses=SimpleNamespace(parse=parse)))
    cfg = tmp_path / 'config.json'
    cfg.write_text(json.dumps({'database': str(tmp_path / 'db.sqlite'), 'output_dir': str(tmp_path / 'output'),
                               'reports_dir': str(tmp_path / 'reports'), 'min_lift': 0}))
    assert main.main(['--weekly', '--config', str(cfg)]) == 0
    output = tmp_path / 'output'
    assert {'weekly_keywords.csv', 'keyword_candidates.csv', 'weekly_report.md'} <= {p.name for p in output.iterdir()}
    rows = list(csv.DictReader((output / 'weekly_keywords.csv').open()))
    assert len(rows) == 14
    for weekday in {r['day'] for r in rows}:
        assert sorted(r['tier'] for r in rows if r['day'] == weekday) == ['Explore', 'Proven']
    assert {r['total_units_sold'] for r in rows if r['tier'] == 'Proven'} == {'18'}
    assert {r['total_units_sold'] for r in rows if r['tier'] == 'Explore'} == {'2'}
    assert len(list((tmp_path / 'reports' / day).glob('*.csv'))) == 2
    assert main.main(['--weekly', '--offline', '--config', str(cfg)]) == 0
    assert len(model_calls) == 1


def test_import_without_credentials_fails_cleanly(tmp_path, monkeypatch, caplog):
    """Regression check: import without credentials fails cleanly.

    Args:
        tmp_path: Pytest temporary directory for isolated databases, configuration files,
            exports, and backups.
        monkeypatch: Pytest patch manager for temporary environment, clock, client, or
            dependency overrides.
        caplog: Pytest log capture fixture used to inspect messages and ensure sensitive values
            are absent.

    Returns:
        None. Successful completion means the assertions passed; unexpected results raise
        AssertionError or fail a pytest exception check.
    """
    for key in ['EBAY_ACCESS_TOKEN', 'EBAY_REFRESH_TOKEN', 'EBAY_CLIENT_ID', 'EBAY_CLIENT_SECRET']:
        monkeypatch.delenv(key, raising=False)
    cfg = tmp_path / 'config.json'
    cfg.write_text(json.dumps({'database': str(tmp_path / 'empty.sqlite')}))
    assert main.main(['--import', '--config', str(cfg), '--env', str(tmp_path / 'missing.env')]) == 1
    assert 'seller OAuth' in caplog.text
