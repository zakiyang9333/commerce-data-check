import copy
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from check_data import check

ROOT = Path(__file__).resolve().parents[1]
SPEC = json.loads((ROOT / 'examples/config.json').read_text())


class Checks(unittest.TestCase):
    def test_healthy(self):
        self.assertEqual(check(ROOT / 'examples/healthy.csv', SPEC, '2026-01-01')['status'], 'healthy')

    def test_known_defects(self):
        report = check(ROOT / 'examples/broken.csv', SPEC, '2026-01-01')
        self.assertEqual({i['code'] for i in report['issues']},
                         {'duplicate_key', 'wrong_date', 'below_min', 'invalid_number'})
        self.assertNotIn('demo-a', json.dumps(report))

    def test_deadline_boundary(self):
        spec = dict(SPEC, ready_after='2026-01-02T10:00:00+08:00')
        with tempfile.TemporaryDirectory() as folder:
            missing = Path(folder) / 'missing.csv'
            self.assertEqual(check(missing, spec, '2026-01-01', datetime.fromisoformat('2026-01-02T09:59:59+08:00'))['status'], 'waiting')
            self.assertEqual(check(missing, spec, '2026-01-01', datetime.fromisoformat('2026-01-02T10:00:00+08:00'))['status'], 'failed')

    def test_empty_requires_explicit_policy(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'empty.csv'
            path.write_text('date,product_id,sales,ad_spend\n')
            self.assertEqual(check(path, SPEC, '2026-01-01')['status'], 'failed')
            self.assertEqual(check(path, dict(SPEC, allow_empty=True), '2026-01-01')['status'], 'valid_empty')
            path.write_text('')
            self.assertEqual(check(path, dict(SPEC, allow_empty=True), '2026-01-01')['status'], 'failed')

    def test_malformed_and_blank_rows(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'bad.csv'
            path.write_text('date,product_id,sales,ad_spend\n2026-01-01,a,1\n2026-01-01,,1,2\n')
            codes = {i['code'] for i in check(path, SPEC, '2026-01-01')['issues']}
            self.assertEqual(codes, {'row_width', 'blank_required'})

    def test_input_unchanged(self):
        path = ROOT / 'examples/healthy.csv'
        before, spec = path.read_bytes(), copy.deepcopy(SPEC)
        check(path, spec, '2026-01-01')
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(spec, SPEC)


if __name__ == '__main__':
    unittest.main()
