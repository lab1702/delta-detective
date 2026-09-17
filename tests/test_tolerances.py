import json
from decimal import Decimal

import duckdb
import pytest

from delta_detective import investigate
from delta_detective.config import InvestigationError, load_config
from delta_detective.wizard import init_config
from test_features import configure
from test_investigation import setup


def test_tolerance_counts_rules_exports_replay_and_exact_metric(tmp_path):
    ref = [(1, '1.00', 'a'), (2, '1.00', 'a'), (3, '1.00', 'a'), (4, '1.00', 'a')]
    cur = [(1, '1.00', 'a'), (2, '1.01', 'a'), (3, '1.02', 'a'), (4, '0.98', 'a')]
    path = setup(tmp_path, ref, cur, compare_fields=['amount'],
                 field_tolerances={'amount': {'absolute': '0.01'}},
                 rules=[dict(name='changed', field='amount', measure='changed_rows', max=1, export={'limit': 10}),
                        dict(name='up', field='amount', measure='increased_rows', max=0, export={'limit': 10}),
                        dict(name='same', field='amount', measure='unchanged_rows', max=0, export={'limit': 10})],
                 report={'evidence_exports': [{'kind': 'field_changed'}, {'kind': 'changed'}]})
    out = tmp_path / 'out'
    data = investigate(path, out)
    f = data['field_changes']['fields'][0]
    assert (f['changed_rows'], f['unchanged_rows'], f['exact_match_rows'], f['within_tolerance_rows']) == (2, 2, 1, 1)
    assert f['value_changed_rows'] == 2
    assert f['increased_rows'] == f['decreased_rows'] == 1
    assert f['percent_changed'] == 50
    assert data['field_changes']['changed_rows'] == 2
    assert data['summary']['delta'] == Decimal('0.01')
    assert [r['observed'] for r in data['rule_checks']['results']] == [2, 1, 2]
    assert {e['kind']: e['rows'] for e in data['evidence_exports'] if e['kind'] != 'rule'} == {'field_changed': 2, 'changed': 3}
    manifest = json.loads((out / 'manifest.json').read_text())
    assert manifest['configuration']['field_tolerances'] == {'amount': {'absolute': '0.01'}}
    assert manifest['field_changes']['fields'][0]['within_tolerance_rows'] == 1
    assert 'Absolute tolerance: 0.01' in (out / 'report.html').read_text()
    with duckdb.connect() as con:
        con.execute((out / 'analysis.sql').read_text())
        assert con.execute('SELECT changed_rows, exact_match_rows, within_tolerance_rows FROM field_0').fetchone() == (2, 1, 1)
        assert con.execute('SELECT k0 FROM rule_0_evidence ORDER BY k0').fetchall() == [(3,), (4,)]
        assert con.execute('SELECT k0 FROM rule_1_evidence').fetchall() == [(3,)]
        assert con.execute('SELECT k0 FROM rule_2_evidence ORDER BY k0').fetchall() == [(1,), (2,)]


def test_nulls_remain_changes_and_other_fields_remain_exact(tmp_path):
    ref = [(1, 1, 'a', None), (2, 1, 'a', 1), (3, 1, 'a', None), (4, 1, 'a', 1)]
    cur = [(1, 1, 'a', 1), (2, 1, 'a', None), (3, 1, 'a', None), (4, 1, 'b', 2)]
    path = setup(tmp_path, ref, cur, schema='id INTEGER, amount INTEGER, category VARCHAR, score INTEGER',
                 compare_fields=['score', 'category'], field_tolerances={'score': {'absolute': 100}})
    data = investigate(path, tmp_path / 'out')
    f = data['field_changes']['fields'][0]
    assert (f['became_null'], f['from_null'], f['both_null'], f['within_tolerance_rows']) == (1, 1, 1, 1)
    assert f['exact_match_rows'] == 1 and f['changed_rows'] == 2
    assert data['field_changes']['changed_rows'] == 3


@pytest.mark.parametrize('typ,a,b,tolerance,changed', [
    ('HUGEINT', str(-(2**127)), str(2**127-1), '1', 1),
    ('UBIGINT', str(2**64-2), str(2**64-1), '1', 0),
    ('DECIMAL(38,18)', '99999999999999999999.999999999999999998', '99999999999999999999.999999999999999999', '0.000000000000000001', 0),
    ('DECIMAL(38,18)', '-99999999999999999999.999999999999999999', '99999999999999999999.999999999999999999', '0.01', 1),
    ('DOUBLE', 1e308, -1e308, '1', 1),
    ('DOUBLE', 1.0, 1.125, '0.125', 0),
    ('INTEGER', 1, 2, '0', 1),
    ('DECIMAL(18,2)', '1.00', '1.01', '0.009', 1),
])
def test_numeric_extremes_and_boundaries(tmp_path, typ, a, b, tolerance, changed):
    path = setup(tmp_path, [(1, 1, 'a', a)], [(1, 1, 'a', b)],
                 schema=f'id INTEGER, amount INTEGER, category VARCHAR, score {typ}',
                 compare_fields=['score'], field_tolerances={'score': {'absolute': tolerance}})
    data = investigate(path, tmp_path / 'out')
    f = data['field_changes']['fields'][0]
    assert f['changed_rows'] == changed
    assert f['within_tolerance_rows'] == 1 - changed


@pytest.mark.parametrize('spec', [None, [], {'missing': {'absolute': 1}}, {'amount': {}},
    {'amount': {'relative': 1}}, {'amount': {'absolute': -1}}, {'amount': {'absolute': True}},
    {'amount': {'absolute': 'nan'}}, {'amount': {'absolute': '1e999'}},
    {'amount': {'absolute': "0'; DROP TABLE reference; --"}}])
def test_invalid_tolerance_config(tmp_path, spec):
    path = setup(tmp_path, [], [], compare_fields=['amount'], field_tolerances=spec)
    with pytest.raises(InvestigationError):
        load_config(path)


def test_nonnumeric_tolerance_does_not_replace_bundle(tmp_path):
    path = setup(tmp_path, [(1, 1, 'a')], [(1, 1, 'b')], compare_fields=['category'])
    out = tmp_path / 'out'
    investigate(path, out)
    before = {p.name: p.read_bytes() for p in out.iterdir()}
    configure(path, field_tolerances={'category': {'absolute': 1}})
    with pytest.raises(InvestigationError, match='numeric comparison field'):
        investigate(path, out, overwrite=True)
    assert {p.name: p.read_bytes() for p in out.iterdir()} == before


def test_uhugeint_without_parquet_type_conversion():
    from delta_detective.tolerances import field_conditions
    conditions = field_conditions('a', 'b', 'UHUGEINT', '1')
    with duckdb.connect() as con:
        con.execute('CREATE TABLE source (a UHUGEINT, b UHUGEINT)')
        con.execute('INSERT INTO source VALUES (?, ?)', [str(2**128-2), str(2**128-1)])
        assert con.execute('SELECT ' + conditions['within_tolerance_rows'] + ' FROM source').fetchone() == (True,)


def test_wizard_tolerance_roundtrip_and_retry(tmp_path):
    setup(tmp_path, [(1, 1, 'a')], [(1, 1.01, 'a')])
    answers = iter(['', '1', '2', '', '', '', 'yes', '', '1', 'yes', '1', '-1', '0.01', '', ''])
    messages = []
    path = init_config(tmp_path / 'reference.parquet', tmp_path / 'current.parquet', tmp_path / 'new.yaml',
                       ask=lambda p: next(answers), tell=messages.append)
    assert list(answers) == []
    assert any('nonnegative' in m for m in messages)
    assert load_config(path)['field_tolerances'] == {'amount': {'absolute': '0.01'}}
    data = investigate(path, tmp_path / 'out')
    assert data['field_changes']['fields'][0]['within_tolerance_rows'] == 1
    assert data['summary']['delta'] == Decimal('0.01')
