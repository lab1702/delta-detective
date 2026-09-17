import csv
import json
import subprocess
import sys
from decimal import Decimal

import duckdb
import pytest
import yaml

from delta_detective import investigate
from delta_detective.config import InvestigationError
from test_investigation import setup, assert_valid


def configure(path, **changes):
    cfg = yaml.safe_load(path.read_text(encoding='utf-8'))
    if 'metrics' in changes:
        cfg.pop('metric', None)
    cfg.update(changes)
    path.write_text(yaml.safe_dump(cfg), encoding='utf-8')
    return path


def fixture(tmp_path):
    path = setup(tmp_path,
                 [(1, 10, 2, 'West', None), (2, 30, 3, 'West', 'web'), (3, 5, 1, 'East', 'web')],
                 [(1, 7, 4, 'West', None), (2, 30, 3, 'South', 'web'), (4, 8, 2, 'East', '<script>')],
                 schema='id INTEGER, amount DECIMAL(18,2), units INTEGER, category VARCHAR, source VARCHAR')
    return configure(path, metrics=[
        dict(name='revenue', aggregate='sum', column='amount', null_policy='error'),
        dict(name='units', aggregate='sum', column='units', null_policy='error'),
        dict(name='rows', aggregate='count')], dimension_groups=[['category', 'source']])


def test_multiple_metrics_groups_and_replay(tmp_path):
    path = fixture(tmp_path)
    out = tmp_path/'out'
    data = investigate(path, out)
    assert [m['metric']['name'] for m in data['metrics']] == ['revenue', 'units', 'rows']
    for item, totals, parts in zip(data['metrics'], [(45, 45), (6, 9), (3, 3)], [(8, -5, -3), (2, -1, 2), (1, -1, 0)]):
        assert_valid(item, totals, parts)
    assert data['summary'] == data['metrics'][0]['summary']
    assert data['metrics'][1]['dimensions'][0]['evidence'] == 'metric_1.dimension_0_display'
    combined = data['dimensions'][1]
    groups = {(r['category']['category'], r['category']['source']): r for r in combined['rows']}
    assert groups['West', None]['contribution'] == -3
    assert groups['West', 'web']['contribution'] == -30
    assert groups['South', 'web']['contribution'] == 30
    assert data['reclassifications'][1]['rows'] == 1
    html = (out/'report.html').read_text(encoding='utf-8')
    assert '<script>' not in html and '&lt;script&gt;' in html
    assert all(name in html for name in ['revenue', 'units', 'rows'])
    assert not list(out.glob('*.csv'))
    with duckdb.connect() as con:
        con.execute((out/'analysis.sql').read_text(encoding='utf-8'))
        assert con.execute('SELECT current_total FROM main.reconciliation').fetchone() == (45,)
        assert con.execute('SELECT current_total FROM metric_1.reconciliation').fetchone() == (9,)
        assert con.execute('SELECT current_total FROM metric_2.reconciliation').fetchone() == (3,)
    manifest = json.loads((out/'manifest.json').read_text(encoding='utf-8'))
    assert len(manifest['metric_reconciliations']) == 3
    result = subprocess.run([sys.executable, '-m', 'delta_detective.cli', 'investigate', str(path), '--out', str(tmp_path/'cli')], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert all('Metric: '+name in result.stdout for name in ['revenue', 'units', 'rows'])


def test_focused_exports_and_deterministic_limits(tmp_path):
    path = fixture(tmp_path)
    configure(path, report={'evidence_exports': [{'kind': 'removed'}, {'kind': 'added'}, {'kind': 'changed'}, {'kind': 'largest_changes', 'limit': 2}]})
    out = tmp_path/'out'
    data = investigate(path, out)
    def rows(name):
        with (out/name).open(encoding='utf-8', newline='') as stream:
            return list(csv.DictReader(stream))
    assert [r['k0'] for r in rows('metric_0_removed_rows.csv')] == ['3']
    assert [r['k0'] for r in rows('metric_0_added_rows.csv')] == ['4']
    assert [r['k0'] for r in rows('metric_0_changed_rows.csv')] == ['1']
    assert [r['k0'] for r in rows('metric_0_largest_changes_rows.csv')] == ['4', '3']
    assert rows('metric_2_changed_rows.csv') == []
    assert [r['k0'] for r in rows('metric_2_largest_changes_rows.csv')] == ['3', '4']
    assert not (out/'raw_rows.csv').exists()
    assert len(data['evidence_exports']) == 12
    with duckdb.connect() as con:
        con.execute((out/'analysis.sql').read_text(encoding='utf-8'))
        assert con.execute('SELECT k0 FROM main.evidence_largest_changes').fetchall() == [(4,), (3,)]


def test_group_other_null_and_empty(tmp_path):
    path = setup(tmp_path, [], [(i, i+1, str(i), None) for i in range(60)],
                 schema='id INTEGER, amount DECIMAL(18,2), category VARCHAR, source VARCHAR')
    configure(path, dimensions=[], dimension_groups=[['category', 'source']])
    data = investigate(path, tmp_path/'out')
    assert_valid(data, (0, 1830), (1830, 0, 0))
    rows = data['dimensions'][0]['rows']
    assert len(rows) == 51 and rows[-1]['is_other'] and rows[-1]['category'] is None
    assert rows[-1]['contribution'] == 55
    assert rows[0]['category'] == {'category': '59', 'source': None}


def test_later_metric_failure_preserves_bundle(tmp_path):
    path = fixture(tmp_path)
    out = tmp_path/'out'
    investigate(path, out)
    before = {p.name: p.read_bytes() for p in out.iterdir()}
    cfg = yaml.safe_load(path.read_text())
    cfg['metrics'][1]['column'] = 'source'
    configure(path, **cfg)
    with pytest.raises(InvestigationError, match='numeric'):
        investigate(path, out, overwrite=True)
    assert {p.name: p.read_bytes() for p in out.iterdir()} == before


@pytest.mark.parametrize('change', [
    {'metrics': []}, {'metrics': [{'name': 'x', 'aggregate': 'count'}]*2},
    {'metrics': [None]}, {'dimension_groups': [['id', 'category']]},
    {'dimension_groups': [['category']]}, {'dimension_groups': [['category', 'source'], ['source', 'category']]},
    {'dimension_groups': 'category'}, {'dimension_groups': [['category', 'category']]},
    {'report': {'evidence_exports': [{'kind': 'largest_changes', 'limit': 0}]}},
    {'report': {'evidence_exports': [{'kind': 'largest_changes', 'limit': True}]}},
    {'report': {'evidence_exports': [{'kind': 'unknown'}]}},
    {'report': {'evidence_exports': [{'kind': 'removed'}]*2}},
])
def test_invalid_feature_config(tmp_path, change):
    path = fixture(tmp_path)
    configure(path, **change)
    with pytest.raises(InvestigationError):
        investigate(path, tmp_path/'out')


def test_mixed_exact_and_approximate(tmp_path):
    path = setup(tmp_path, [(1, '0.10', 0.1, 'a')], [(1, '0.30', 0.3, 'a')],
                 schema='id INTEGER, amount DECIMAL(18,2), approximate DOUBLE, category VARCHAR')
    configure(path, metrics=[dict(name='exact', aggregate='sum', column='amount', null_policy='error'),
                             dict(name='approx', aggregate='sum', column='approximate', null_policy='error')])
    data = investigate(path, tmp_path/'out')
    assert data['metrics'][0]['summary']['delta'] == Decimal('.20')
    assert data['metrics'][0]['summary']['exact'] is True
    assert data['metrics'][1]['summary']['exact'] is False
    assert data['metrics'][1]['summary']['delta'] == pytest.approx(.2)


def test_empty_combined_group_and_single_metric_exports(tmp_path):
    path = setup(tmp_path, [], [], schema='id INTEGER, amount DECIMAL(18,2), category VARCHAR, source VARCHAR')
    configure(path, dimension_groups=[['category', 'source']],
              report={'include_raw_rows': True, 'evidence_exports': [{'kind': 'largest_changes'}]})
    out = tmp_path/'out'
    data = investigate(path, out)
    assert_valid(data, (0, 0), (0, 0, 0))
    assert data['dimensions'][1]['rows'] == []
    assert (out/'raw_rows.csv').is_file()
    assert (out/'largest_changes_rows.csv').read_text().count('\n') == 1
    assert data['evidence_exports'][1]['limit'] == 100


def test_later_arithmetic_failure_after_export_is_atomic(tmp_path):
    path = setup(tmp_path, [], [(1, 1, '9'*38, 'a'), (2, 1, '9'*38, 'b')],
                 schema='id INTEGER, amount INTEGER, huge DECIMAL(38,0), category VARCHAR')
    out = tmp_path/'out'
    investigate(path, out)
    before = {p.name: p.read_bytes() for p in out.iterdir()}
    configure(path, metrics=[dict(name='small', aggregate='sum', column='amount', null_policy='error'),
                             dict(name='overflow', aggregate='sum', column='huge', null_policy='error')],
              report={'evidence_exports': [{'kind': 'added'}]})
    with pytest.raises(InvestigationError, match='overflow'):
        investigate(path, out, overwrite=True)
    assert {p.name: p.read_bytes() for p in out.iterdir()} == before
    assert not list(tmp_path.glob('.delta-*'))


def test_composite_keys_and_exact_export_ties(tmp_path):
    path = setup(tmp_path, [(1, 'b', 10, 'a'), (1, 'a', 10, 'a')],
                 [(1, 'b', 8, 'a'), (1, 'a', 12, 'a')],
                 schema='id INTEGER, sub VARCHAR, amount DECIMAL(18,2), category VARCHAR', key=['id', 'sub'])
    configure(path, report={'evidence_exports': [{'kind': 'changed', 'limit': 1}]})
    out = tmp_path/'out'
    investigate(path, out)
    with (out/'changed_rows.csv').open(newline='') as stream:
        rows = list(csv.DictReader(stream))
    assert [(r['k0'], r['k1'], r['contribution']) for r in rows] == [('1', 'a', '2.00')]


def test_metric_and_metrics_are_mutually_exclusive(tmp_path):
    path = setup(tmp_path, [], [])
    cfg = yaml.safe_load(path.read_text())
    cfg['metrics'] = [cfg['metric']]
    path.write_text(yaml.safe_dump(cfg))
    with pytest.raises(InvestigationError, match='exactly one'):
        investigate(path, tmp_path/'out')
