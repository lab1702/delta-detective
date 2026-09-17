import json
from datetime import date

import duckdb
import pytest

from delta_detective import investigate
from delta_detective.config import InvestigationError, load_config
from test_features import configure
from test_investigation import setup
from test_csv_options import csv_config


def filt(column='category', operator='equals', value='East'):
    result = dict(column=column, operator=operator)
    if operator not in ('is_null', 'is_not_null'):
        result['value'] = value
    return result


def test_population_changes_counts_rules_exports_and_replay(tmp_path):
    path = setup(tmp_path, [(1, 10, 'East'), (2, 20, 'West'), (3, 30, None)],
                 [(1, 10, 'West'), (2, 25, 'East'), (3, 30, None)],
                 filters=[filt()], compare_fields=['category'],
                 rules=[dict(name='limit', metric='amount', measure='current_rows', max=1)],
                 report={'include_raw_rows': True})
    out = tmp_path / 'out'
    data = investigate(path, out)
    s = data['summary']
    assert (s['delta'], s['matched_rows'], s['added_rows'], s['removed_rows']) == (15, 0, 1, 1)
    assert data['rule_checks']['status'] == 'passed'
    assert data['field_changes']['matched_rows'] == 0
    scope = data['filter_scope']
    assert scope['status'] == 'applied'
    for counts in scope['inputs'].values():
        assert (counts['total_rows'], counts['included_rows'], counts['excluded_rows']) == (3, 1, 2)
    manifest = json.loads((out / 'manifest.json').read_text(encoding='utf-8'))
    assert manifest['filter_scope'] == scope
    assert json.loads((out / 'findings.json').read_text())['filter_scope'] == scope
    assert 'Records entering or leaving' in (out / 'report.html').read_text(encoding='utf-8')
    with duckdb.connect() as con:
        con.execute((out / 'analysis.sql').read_text(encoding='utf-8'))
        assert con.execute('SELECT id FROM reference').fetchall() == [(1,)]
        assert con.execute('SELECT id FROM current').fetchall() == [(2,)]
        assert con.execute('SELECT * FROM reference_filter_scope').fetchone() == (3, 1)
        rows = con.execute('SELECT * FROM read_csv(?, header=true)', [str(out / 'raw_rows.csv')]).fetchall()
        assert len(rows) == 2


@pytest.mark.parametrize('operator,value,expected', [
    ('equals', 'a', 1), ('in', ['a', 'b'], 2), ('is_null', None, 1), ('is_not_null', None, 2),
])
def test_text_membership_and_nulls(tmp_path, operator, value, expected):
    rows = [(1, 1, 'a'), (2, 2, 'b'), (3, 3, None)]
    path = setup(tmp_path, rows, rows, filters=[filt(operator=operator, value=value)])
    assert investigate(path, tmp_path / 'out')['summary']['current_rows'] == expected


@pytest.mark.parametrize('operator,value,expected', [
    ('gt', '1.005', 1), ('gte', '1.01', 1), ('lt', '1.01', 1), ('lte', '1.00', 1),
    ('equals', '1.005', 0), ('in', ['1.00', '1.01'], 2),
])
def test_numeric_bounds_do_not_round_to_column_scale(tmp_path, operator, value, expected):
    rows = [(1, '1.00', 'a'), (2, '1.01', 'b')]
    path = setup(tmp_path, rows, rows, filters=[filt('amount', operator, value)])
    data = investigate(path, tmp_path / 'out')
    assert data['summary']['current_rows'] == expected


def test_dates_booleans_and_conditions(tmp_path):
    rows = [(1, 1, 'a', date(2025, 1, 1), True), (2, 2, 'a', date(2026, 1, 1), True),
            (3, 3, 'a', date(2026, 1, 1), False)]
    path = setup(tmp_path, rows, rows,
                 schema='id INTEGER, amount INTEGER, category VARCHAR, day DATE, active BOOLEAN',
                 filters=[filt('day', 'gte', '2026-01-01'), filt('active', 'equals', True)])
    assert investigate(path, tmp_path / 'out')['summary']['current_total'] == 2


def test_csv_mapping_precedes_filtering_and_empty_subset(tmp_path):
    path = csv_config(tmp_path, 'id,amount,region\n1,10,West\n',
                      options={s: {'header': True, 'types': {'id': 'INTEGER', 'amount': 'INTEGER', 'region': 'VARCHAR'}}
                               for s in ('reference', 'current')})
    configure(path, column_mapping={s: {'region': 'category'} for s in ('reference', 'current')}, filters=[filt()])
    data = investigate(path, tmp_path / 'out')
    assert data['summary']['current_rows'] == data['summary']['reference_rows'] == 0
    assert data['summary']['delta'] == 0


@pytest.mark.parametrize('filters', [None, {}, [None], [dict(column='category', operator='sql', value='true')],
    [filt(value=None)], [filt(operator='in', value=[])], [filt(operator='in', value='East')],
    [filt(value=float('nan'))], [dict(column='category', operator='is_null', value=None)],
    [dict(column='category', operator='equals')], [filt(value={'nested': 'value'})]])
def test_invalid_filter_configuration(tmp_path, filters):
    path = setup(tmp_path, [], [], filters=filters)
    with pytest.raises(InvestigationError):
        load_config(path)


@pytest.mark.parametrize('filter_spec', [filt('missing'), filt(value=1), filt('amount', value=True),
    filt('amount', value='not numeric'), filt(operator='gt'), filt('amount', value='1e999')])
def test_invalid_typed_filter_preserves_prior_bundle(tmp_path, filter_spec):
    path = setup(tmp_path, [(1, 1, 'a')], [(1, 2, 'a')])
    out = tmp_path / 'out'
    investigate(path, out)
    before = {p.name: p.read_bytes() for p in out.iterdir()}
    configure(path, filters=[filter_spec])
    with pytest.raises(InvestigationError):
        investigate(path, out, overwrite=True)
    assert {p.name: p.read_bytes() for p in out.iterdir()} == before


def test_quoted_value_is_literal_and_html_escaped(tmp_path):
    value = "<script>' OR true --"
    rows = [(1, 1, value), (2, 2, 'other')]
    path = setup(tmp_path, rows, rows, filters=[filt(value=value)])
    out = tmp_path / 'out'
    assert investigate(path, out)['summary']['current_rows'] == 1
    assert '<script>' not in (out / 'report.html').read_text(encoding='utf-8')


def test_schema_failure_does_not_claim_filter_applied(tmp_path):
    path = setup(tmp_path, [], [], filters=[filt()])
    configure(path, schema={'columns': {'missing': None}})
    out = tmp_path / 'out'
    data = investigate(path, out)
    assert data['filter_scope']['status'] == 'not_evaluated'
    assert data['filter_scope']['inputs'] == {}
    assert 'filters were not applied' in (out / 'report.html').read_text(encoding='utf-8')


def test_validation_applies_to_included_rows(tmp_path):
    rows = [(1, 1, 'East'), (2, None, 'West'), (2, 2, 'West')]
    path = setup(tmp_path, rows, rows, filters=[filt()])
    assert investigate(path, tmp_path / 'out')['summary']['current_total'] == 1
    configure(path, filters=[filt(value='West')])
    with pytest.raises(InvestigationError, match='duplicate keys'):
        investigate(path, tmp_path / 'bad')


@pytest.mark.parametrize('value', ['2026-02-30', '20260101', 20260101])
def test_invalid_dates(tmp_path, value):
    path = setup(tmp_path, [], [], schema='id INTEGER, amount INTEGER, category VARCHAR, day DATE',
                 filters=[filt('day', 'gte', value)])
    with pytest.raises(InvestigationError):
        investigate(path, tmp_path / 'out')


def test_filter_column_type_mismatch_even_when_not_selected(tmp_path):
    path = csv_config(tmp_path, 'id,amount,category,active\n1,2,a,true\n',
                      options={'reference': {'header': True, 'types': {'active': 'BOOLEAN'}},
                               'current': {'header': True, 'types': {'active': 'VARCHAR'}}})
    configure(path, filters=[filt('active', 'equals', True)])
    with pytest.raises(InvestigationError, match='matching types'):
        investigate(path, tmp_path / 'out')


def test_filter_does_not_hide_malformed_csv(tmp_path):
    path = csv_config(tmp_path, 'id,amount,category\n1,2,East\n2,3,West,extra\n',
                      options={s: {'header': True, 'delimiter': ','} for s in ('reference', 'current')})
    configure(path, filters=[filt()])
    with pytest.raises(InvestigationError):
        investigate(path, tmp_path / 'out')
