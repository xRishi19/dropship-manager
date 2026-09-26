"""Atomic per-file exports with spreadsheet-safe text cells."""
import csv
import io
import json
import os
import re
import tempfile
from pathlib import Path

WEEKLY_FIELDS = ['day', 'tier', 'keyword', 'rank', 'confidence', 'total_units_sold',
                 'recent_units_sold', 'distinct_products', 'trend', 'reason']

NUMBER = re.compile(r'-?\d+(\.\d+)?')


def atomic_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def csv_text(rows, fields):
    buffer = io.StringIO(newline='')
    writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction='ignore')
    writer.writeheader()
    for row in rows:
        safe = {}
        for key, value in row.items():
            if isinstance(value, (list, dict)):
                value = json.dumps(value, ensure_ascii=False)
            # Escape formula-like text, but keep plain numbers (e.g. a loss of -11.00) numeric.
            if (isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@'))
                    and not NUMBER.fullmatch(value.strip())):
                value = "'" + value
            safe[key] = value
        writer.writerow(safe)
    return buffer.getvalue()


def export_analysis(directory, candidates, listings, diagnostics=None):
    directory = Path(directory)
    fields = list(candidates[0]) if candidates else ['keyword', 'total_units_sold', 'recent_units_sold']
    atomic_write(directory / 'keyword_candidates.csv', csv_text(candidates, fields))
    fields = list(listings[0]) if listings else ['item_id', 'title', 'total_units_sold']
    atomic_write(directory / 'listing_metrics.csv', csv_text(listings, fields))
    if diagnostics is not None:
        atomic_write(directory / 'candidate_diagnostics.json', json.dumps(diagnostics, indent=2))


def export_week(directory, payload):
    directory = Path(directory)
    export_analysis(directory, payload['candidates'], payload['listings'], payload.get('diagnostics'))
    atomic_write(directory / 'weekly_keywords.csv', csv_text(payload['results'], WEEKLY_FIELDS))
    atomic_write(directory / 'weekly_keywords.json', json.dumps(payload, indent=2, ensure_ascii=False))
    def clean(value):
        return '' if value is None else str(value).replace('|', '\\|').replace('\n', ' ')
    if payload.get('selection_method') == 'fallback':
        method = ['**GPT selection failed; these are the top deterministic Python scores per tier (no GPT review).**']
    else:
        method = ['Rank is GPT semantic preference within each tier over Python-calculated candidates.',
                  'Confidence is subjective sourcing confidence, not a statistical probability.']
    asins = payload.get('asins')
    pulls = [f'Each day: **Proven** — pull {asins["Proven"]} ASINs; **Explore** — pull {asins["Explore"]} ASINs.', ''] if asins else []
    lines = [f'# Sourcing week of {payload["week"]}', '',
             f'Analysis through {payload["as_of"]} (UTC). Model: {payload["model"]}.', '', *pulls, *method,
             'Proven keywords already sell well; Explore keywords test newer products and markets.',
             'Lift is units per listing-day versus the store average (1.0 = average).', '',
             '| Day | Tier | Rank | Keyword | Market | Status | Units | Recent | Products | Lift | Trend | Reason |',
             '|---|---|---:|---|---|---|---:|---:|---:|---:|---|---|']
    for row in payload['results']:
        lines.append('| ' + ' | '.join(clean(row.get(k)) for k in ['day', 'tier', 'rank', 'keyword', 'market', 'status', 'total_units_sold', 'recent_units_sold', 'distinct_products', 'sell_through_lift', 'trend', 'reason']) + ' |')
    lines += ['', '## Resting explore keywords', '',
              'Explored in recent weeks; skipped while their 60–90 day tests run: ' + (', '.join(payload.get('resting') or []) or 'None.')]
    lines += ['', '## Past keyword results', '',
              'Listings started during each recommendation week whose titles contain the keyword.', '']
    if payload.get('outcomes'):
        lines += ['| Week | Tier | Keyword | New listings | Sold | Units |', '|---|---|---|---:|---:|---:|']
        for week in payload['outcomes']:
            for item in week['keywords']:
                prefix = f'| {week["week"]} | {clean(item.get("tier"))} | {clean(item["keyword"])} |'
                if item.get('outcome') == 'no_data':
                    lines.append(prefix + ' no data | | |')
                else:
                    lines.append(prefix + f' {item["new_listings"]} | {item["new_listings_sold"]} | {item["units_sold"]} |')
    else:
        lines.append('No earlier recommendation weeks.')
    lines += ['', '## Dropped since previous recommendation week', '',
              ', '.join(payload['dropped']) or 'None.', '', '## Evidence strength', '',
              'Limited-volume selections (2–4 observed units): ' +
              (', '.join(r['keyword'] for r in payload['results'] if r['total_units_sold'] < 5) or 'None.') + '.',
              'Explore picks are expected to be limited-volume leads. Even 5+ units do not establish statistical significance.',
              '', '## Candidate filtering', '',
              '```json', json.dumps(payload.get('diagnostics', {}), indent=2), '```', '', '## Data coverage', '',
              json.dumps(payload['coverage'], indent=2), '',
              'Mean/median units and unsold counts are available in keyword_candidates.csv.',
              'Counts describe observed history, not guaranteed lifetime sales or conversion rates.',
              'Days are assignment slots for the Monday–Sunday week containing the analysis date.', '']
    atomic_write(directory / 'weekly_report.md', '\n'.join(lines))
