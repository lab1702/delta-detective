import csv
import json
from decimal import Decimal

import duckdb
import pytest

from delta_detective import investigate
from test_features import configure
from test_investigation import setup


def test_movements_include_only_matched_category_changes(tmp_path):
    path = setup(tmp_path,
                 [(1, 30, 'West'), (2, 12, 'East'), (3, 8, 'East'), (4, 5, 'gone'), (5, 2, 'same')],
                 [(1, 30, 'South'), (2, '10.5', 'West'), (3, 8, 'West'), (5, 3, 'same'), (6, 9, 'new')])
    out = tmp_path/'out'
    data = investigate(path, out)
    moves = data['reclassifications'][0]['movements']
    assert moves['rows'] == [
        dict(from_category='East', to_category='West', rows=2, also_metric_changed=1,
             reference_amount=Decimal(20), current_amount=Decimal('18.5'), is_other=False),
        dict(from_category='West', to_category='South', rows=1, also_metric_changed=0,
             reference_amount=Decimal(30), current_amount=Decimal(30), is_other=False)]
    assert all(c['status'] == 'passed' for c in moves['checks'].values())
    assert moves['evidence'] == 'main.movement_0_display'
    assert not list(out.glob('*.csv'))
    with duckdb.connect() as con:
        con.execute((out/'analysis.sql').read_text(encoding='utf-8'))
        assert con.execute('SELECT sum(rows), sum(also_metric_changed), sum(reference_amount), sum(current_amount) FROM main.movement_0').fetchone() == (3, 1, 50, Decimal('48.5'))


def test_nulls_combined_groups_count_and_exports(tmp_path):
    path = setup(tmp_path,
                 [(1, 10, None, 'web'), (2, 20, '(NULL)', 'web'), (3, 30, 'stay', None)],
                 [(1, 10, '(NULL)', 'store'), (2, 18, '<script>', 'web'), (3, 30, 'stay', 'web')],
                 schema='id INTEGER, amount DECIMAL(18,2), category VARCHAR, source VARCHAR')
    configure(path, metrics=[dict(name='amount', aggregate='sum', column='amount', null_policy='error'),
                             dict(name='rows', aggregate='count')], dimension_groups=[['category', 'source']],
              report={'evidence_exports': [{'kind': 'moved'}]})
    out = tmp_path/'out'
    data = investigate(path, out)
    singles = data['reclassifications'][0]['movements']['rows']
    assert singles[0]['from_category'] is None
    assert singles[0]['to_category'] == '(NULL)'
    combined = data['reclassifications'][1]['movements']['rows']
    assert len(combined) == 3
    assert sum(r['rows'] for r in combined) == 3
    assert any(r['from_category'] == {'category': None, 'source': 'web'} for r in combined)
    counts = data['metrics'][1]['reclassifications'][1]['movements']
    assert counts['evidence'] == 'metric_1.movement_1_display'
    assert all(r['reference_amount'] == r['current_amount'] == r['rows'] for r in counts['rows'])
    assert all(r['also_metric_changed'] == 0 for r in counts['rows'])
    with (out/'metric_0_moved_rows.csv').open(newline='') as stream:
        rows = list(csv.DictReader(stream))
    assert [r['k0'] for r in rows] == ['2', '1', '3']
    assert [r['contribution'] for r in rows] == ['-2.00', '0.00', '0.00']
    assert data['evidence_exports'][0]['rows'] == 3  # One export row per key, despite overlap.
    html = (out/'report.html').read_text(encoding='utf-8')
    assert '<script>' not in html and '&lt;script&gt;' in html
    assert 'NULL (missing value)' in html and 'Value: (NULL)' in html
    manifest = json.loads((out/'manifest.json').read_text(encoding='utf-8'))
    assert manifest['raw_evidence']['exports'][0]['kind'] == 'moved'
    with duckdb.connect() as con:
        con.execute((out/'analysis.sql').read_text(encoding='utf-8'))
        assert con.execute('SELECT count(*) FROM metric_1.evidence_moved').fetchone() == (3,)


@pytest.mark.parametrize('grouped', [False, True])
def test_top_50_other_preserves_movement_totals(tmp_path, grouped):
    path = setup(tmp_path, [(i, i+1, f'old{i:02}', None) for i in range(60)],
                 [(i, i+2, f'new{i:02}', 'web') for i in range(60)],
                 schema='id INTEGER, amount DECIMAL(18,2), category VARCHAR, source VARCHAR')
    if grouped:
        configure(path, dimensions=[], dimension_groups=[['category', 'source']])
    data = investigate(path, tmp_path/'out')
    moves = data['reclassifications'][0]['movements']
    rows = moves['rows']
    assert len(rows) == 51
    assert rows[-1]['is_other'] and rows[-1]['from_category'] is None and rows[-1]['to_category'] is None
    assert rows[-1]['rows'] == rows[-1]['also_metric_changed'] == 10
    assert rows[-1]['reference_amount'] == 555
    assert rows[-1]['current_amount'] == 565
    assert sum(r['reference_amount'] for r in rows) == 1830
    assert sum(r['current_amount'] for r in rows) == 1890
    assert all(c['residual'] == 0 for c in moves['checks'].values())


@pytest.mark.parametrize('dimensions', [[], ['category']])
def test_empty_movements_and_header_only_exports(tmp_path, dimensions):
    path = setup(tmp_path, [(1, 1, 'a')], [(1, 2, 'a')], dimensions=dimensions)
    configure(path, report={'evidence_exports': [{'kind': 'moved', 'limit': 1}]})
    out = tmp_path/'out'
    data = investigate(path, out)
    assert all(r['movements']['rows'] == [] for r in data['reclassifications'])
    assert (out/'moved_rows.csv').read_text().count('\n') == 1
    assert data['evidence_exports'][0]['rows'] == 0


def test_signed_float_movements_and_export_limit(tmp_path):
    path = setup(tmp_path, [(1, -.1, 'a'), (2, .2, 'a')], [(1, -.3, 'b'), (2, .2, 'b')],
                 schema='id INTEGER, amount DOUBLE, category VARCHAR')
    configure(path, report={'evidence_exports': [{'kind': 'moved', 'limit': 1}]})
    out = tmp_path/'out'
    data = investigate(path, out)
    moves = data['reclassifications'][0]['movements']
    assert moves['rows'][0]['reference_amount'] == pytest.approx(.1)
    assert moves['rows'][0]['current_amount'] == pytest.approx(-.1)
    assert all(c['status'] == 'passed' and not c['exact'] for c in moves['checks'].values())
    with (out/'moved_rows.csv').open(newline='') as stream:
        assert [r['k0'] for r in csv.DictReader(stream)] == ['1']


def test_numeric_categories_and_high_precision_amounts(tmp_path):
    before = '99999999999999999999999999999.01'
    after = '99999999999999999999999999999.02'
    path = setup(tmp_path, [(987654321, before, 1)], [(987654321, after, 2)],
                 schema='id INTEGER, amount DECIMAL(38,2), category INTEGER')
    out = tmp_path/'out'
    data = investigate(path, out)
    movement = data['reclassifications'][0]['movements']['rows'][0]
    assert movement['from_category'] == 1 and movement['to_category'] == 2
    assert movement['reference_amount'] == Decimal(before)
    assert movement['current_amount'] == Decimal(after)
    for file in out.iterdir():
        assert '987654321' not in file.read_text(encoding='utf-8')
