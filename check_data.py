#!/usr/bin/env python3
"""Read-only daily CSV checks. Python 3.9+, standard library only."""
import argparse
import csv
import json
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path


def check(path, spec, target, now=None):
    target = date.fromisoformat(target).isoformat()
    now = now or datetime.now().astimezone()
    deadline = datetime.fromisoformat(spec['ready_after']) if spec.get('ready_after') else None
    if deadline is not None and deadline.utcoffset() is None:
        raise ValueError('ready_after must include a timezone offset')
    if now.utcoffset() is None:
        raise ValueError('now must include a timezone offset')
    issues = []
    def issue(code, row=None, column=None):
        item = {'code': code}
        if row is not None:
            item['row'] = row
        if column is not None:
            item['column'] = column
        issues.append(item)
    def result(state, rows=0):
        return {'status': state, 'target_date': target, 'rows': rows, 'issues': issues}
    path = Path(path)
    if not path.exists():
        if deadline and now < deadline:
            return result('waiting')
        issue('missing_file')
        return result('failed')
    with path.open(encoding='utf-8-sig', newline='') as stream:
        records = list(csv.reader(stream, strict=True))
    if not records:
        issue('missing_header')
        return result('failed')
    header, rows = records[0], [r for r in records[1:] if any(v.strip() for v in r)]
    if len(set(header)) != len(header) or any(not h.strip() for h in header):
        issue('invalid_header')
        return result('failed', len(rows))
    required = set(spec.get('required_columns', [])) | {spec['date_column']}
    required.update(spec.get('key_columns', []))
    required.update(spec.get('numeric_columns', {}))
    for col in sorted(required - set(header)):
        issue('missing_column', column=col)
    if issues:
        return result('failed', len(rows))
    if not rows:
        if spec.get('allow_empty') is True:
            return result('valid_empty')
        issue('empty_data')
        return result('failed')
    seen = set()
    for number, values in enumerate(rows, 1):
        if len(values) != len(header):
            issue('row_width', number)
            continue
        record = dict(zip(header, values))
        for col in sorted(required):
            if not record[col].strip():
                issue('blank_required', number, col)
        try:
            actual = date.fromisoformat(record[spec['date_column']]).isoformat()
            if actual != target:
                issue('wrong_date', number, spec['date_column'])
        except ValueError:
            issue('invalid_date', number, spec['date_column'])
        keys = spec.get('key_columns', [])
        if keys and all(record[k].strip() for k in keys):
            key = tuple(record[k].strip() for k in keys)
            if key in seen:
                issue('duplicate_key', number)
            seen.add(key)
        for col, rules in spec.get('numeric_columns', {}).items():
            try:
                value = Decimal(record[col].strip())
                if not value.is_finite():
                    raise InvalidOperation
                if 'min' in rules and value < Decimal(str(rules['min'])):
                    issue('below_min', number, col)
                if 'max' in rules and value > Decimal(str(rules['max'])):
                    issue('above_max', number, col)
            except InvalidOperation:
                issue('invalid_number', number, col)
    return result('failed' if issues else 'healthy', len(rows))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('csv_file')
    parser.add_argument('--config', required=True)
    parser.add_argument('--date', required=True, help='YYYY-MM-DD')
    args = parser.parse_args()
    try:
        spec = json.loads(Path(args.config).read_text(encoding='utf-8'))
        report = check(args.csv_file, spec, args.date)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, csv.Error):
        report = {'status': 'error', 'issues': [{'code': 'unreadable_input_or_invalid_config'}]}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return {'healthy': 0, 'valid_empty': 0, 'failed': 1, 'waiting': 2, 'error': 3}[report['status']]


if __name__ == '__main__':
    raise SystemExit(main())
