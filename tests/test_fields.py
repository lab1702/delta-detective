import csv
import json
from datetime import date

import duckdb
import pytest

from delta_detective import investigate
from delta_detective.config import InvestigationError
from delta_detective.loading import literal
from test_features import configure
from test_investigation import setup


def test_field_changes_and_privacy(tmp_path):
    path = setup(tmp_path,
                 [(1, 10, 'a', 'secret_old'), (2, 10, 'a', None), (3, 10, 'a', 'x'),
                  (4, 10, 'a', ''), (5, 10, 'a', None), (6, 10, 'a', 'removed')],
                 [(1, 10, 'a', 'secret_new'), (2, 10, 'a', ''), (3, 10, 'a', None),
                  (4, 10, 'a', ' '), (5, 10, 'a', None), (7, 10, 'a', 'added')],
                 schema='id INTEGER, amount INTEGER, category VARCHAR, description VARCHAR')
    configure(path, compare_fields=['description'])
    out = tmp_path/'out'
    data = investigate(path, out)
    assert data['summary']['delta'] == 0
    changes = data['field_changes']
    assert (changes['matched_rows'], changes['changed_rows'], changes['unchanged_rows']) == (5, 4, 1)
    field = changes['fields'][0]
    assert {k: field[k] for k in ['changed_rows', 'unchanged_rows', 'became_null', 'from_null', 'both_null', 'value_changed_rows', 'became_blank', 'from_blank']} == dict(
        changed_rows=4, unchanged_rows=1, became_null=1, from_null=1, both_null=1, value_changed_rows=2, became_blank=1, from_blank=1)
    assert field['percent_changed'] == 80
    for file in out.iterdir():
        assert 'secret_old' not in file.read_text(encoding='utf-8')
        assert 'secret_new' not in file.read_text(encoding='utf-8')
    with duckdb.connect() as con:
        con.execute((out/'analysis.sql').read_text(encoding='utf-8'))
        assert con.execute('SELECT * FROM main.field_overview').fetchone() == (5, 4)
        assert con.execute('SELECT became_blank FROM main.field_0').fetchone() == (1,)
    manifest = json.loads((out/'manifest.json').read_text())
    assert manifest['field_changes']['fields'][0]['percent_changed'] == '80'


def test_date_numeric_boolean_and_multiple_metrics(tmp_path):
    path = setup(tmp_path, [(1, 10, 'a', date(2026, 1, 1), True), (2, 10, 'a', date(2026, 1, 5), False)],
                 [(1, 12, 'a', date(2026, 1, 3), False), (2, 9, 'a', date(2026, 1, 4), False)],
                 schema='id INTEGER, amount DECIMAL(18,2), category VARCHAR, delivery DATE, active BOOLEAN')
    configure(path, compare_fields=['delivery', 'amount', 'active'], metrics=[dict(name='amount', aggregate='sum', column='amount', null_policy='error'), dict(name='rows', aggregate='count')])
    data = investigate(path, tmp_path/'out')
    fields = data['field_changes']['fields']
    assert data['field_changes']['changed_rows'] == 2
    assert len(fields) == 3
    assert fields[0]['increased_rows'] == fields[0]['decreased_rows'] == 1
    assert fields[1]['increased_rows'] == fields[1]['decreased_rows'] == 1
    assert fields[2]['changed_rows'] == 1 and 'increased_rows' not in fields[2]


def test_exports_include_zero_metric_change_and_limit(tmp_path):
    path = setup(tmp_path, [(1, 10, 'a'), (2, 10, 'b'), (3, 10, 'c')], [(1, 10, 'x'), (2, 10, 'y'), (4, 10, 'z')])
    configure(path, compare_fields=['category'], dimensions=[],
              report={'include_raw_rows': True, 'evidence_exports': [{'kind': 'field_changed', 'limit': 1}]})
    out = tmp_path/'out'
    data = investigate(path, out)
    with (out/'field_changed_rows.csv').open(newline='') as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 1
    assert (rows[0]['k0'], rows[0]['rf0'], rows[0]['cf0'], rows[0]['contribution']) == ('1', 'a', 'x', '0.00')
    with (out/'raw_rows.csv').open(newline='') as stream:
        assert 'rf0' in csv.DictReader(stream).fieldnames
    assert data['field_changes']['changed_rows'] == 2
    manifest = json.loads((out/'manifest.json').read_text())
    assert manifest['raw_evidence']['compare_fields'] == ['category']


@pytest.mark.parametrize('fields', [None, 'category', ['category', 'category'], ['id'], ['missing']])
def test_invalid_fields(tmp_path, fields):
    path = setup(tmp_path, [], [])
    configure(path, compare_fields=fields)
    with pytest.raises(InvestigationError):
        investigate(path, tmp_path/'out')
    assert not (tmp_path/'out').exists()


def test_type_mismatch_and_unsupported_type(tmp_path):
    path = setup(tmp_path, [(1, 10, 'a', [1])], [(1, 10, 'a', [2])],
                 schema='id INTEGER, amount INTEGER, category VARCHAR, extra INTEGER[]')
    configure(path, compare_fields=['extra'])
    with pytest.raises(InvestigationError, match='Unsupported compare_fields'):
        investigate(path, tmp_path/'out')
    with duckdb.connect() as con:
        con.execute(f"COPY (SELECT 1 AS id, 10 AS amount, 'a' AS category, 'x' AS extra) TO {literal(tmp_path/'current.parquet')} (FORMAT PARQUET)")
    with pytest.raises(InvestigationError, match='Incompatible selected column types'):
        investigate(path, tmp_path/'out')


def test_no_matches_and_empty_export(tmp_path):
    path = setup(tmp_path, [(1, 10, 'a')], [(2, 10, 'b')])
    configure(path, compare_fields=['category'], report={'evidence_exports': [{'kind': 'field_changed'}]})
    out = tmp_path/'out'
    data = investigate(path, out)
    assert data['field_changes']['matched_rows'] == 0
    assert data['field_changes']['fields'][0]['percent_changed'] is None
    assert (out/'field_changed_rows.csv').read_text().count('\n') == 1


def test_float_comparison_is_exact_and_nonfinite_rejected(tmp_path):
    path = setup(tmp_path, [(1, 1, 'a', 1.)], [(1, 1, 'a', 1.0000000001)],
                 schema='id INTEGER, amount INTEGER, category VARCHAR, reading DOUBLE')
    configure(path, compare_fields=['reading'])
    data = investigate(path, tmp_path/'out')
    assert data['field_changes']['fields'][0]['changed_rows'] == 1
    with duckdb.connect() as con:
        con.execute(f"COPY (SELECT 1 AS id, 1 AS amount, 'a' AS category, 'NaN'::DOUBLE AS reading) TO {literal(tmp_path/'current.parquet')} (FORMAT PARQUET)")
    with pytest.raises(InvestigationError, match='nonfinite comparison field'):
        investigate(path, tmp_path/'bad')


def test_schema_failure_skips_field_comparison(tmp_path):
    path = setup(tmp_path, [], [])
    configure(path, compare_fields=['missing'], schema={'columns': {'missing': None}})
    assert investigate(path, tmp_path/'out')['field_changes']['status'] == 'not_evaluated'


def test_unusual_field_names_and_exact_decimal(tmp_path):
    path = setup(tmp_path, [(1, 1, 'a', '99999999999999999999999999999.01')],
                 [(1, 1, 'a', '99999999999999999999999999999.02')],
                 schema='id INTEGER, amount INTEGER, category VARCHAR, "<extra>" DECIMAL(38,2)')
    configure(path, compare_fields=['<extra>'])
    out = tmp_path/'out'
    assert investigate(path, out)['field_changes']['fields'][0]['increased_rows'] == 1
    assert '&lt;extra&gt;' in (out/'report.html').read_text()
