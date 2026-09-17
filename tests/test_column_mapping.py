import json
from decimal import Decimal

import duckdb
import pytest

from delta_detective import investigate
from delta_detective.config import InvestigationError, load_config
from delta_detective.loading import ident, literal, load_snapshots
from test_features import configure
from test_investigation import setup
from test_csv_options import csv_config


def test_parquet_mapping_before_contract_comparison_and_replay(tmp_path):
    path = setup(tmp_path, [(1, 10, 'a')], [(1, 12, 'b')])
    with duckdb.connect() as con:
        con.execute(f"COPY (SELECT id AS customerId, amount AS netAmount, category AS region "
                    f"FROM read_parquet({literal(tmp_path / 'current.parquet')})) "
                    f"TO {literal(tmp_path / 'renamed.parquet')} (FORMAT PARQUET)")
    mapping = {'customerId': 'id', 'netAmount': 'amount', 'region': 'category'}
    configure(path, current='renamed.parquet', column_mapping={'current': mapping},
              schema={'columns': {'id': 'INTEGER', 'amount': 'DECIMAL(18,2)', 'category': 'VARCHAR'},
                      'allow_extra_columns': False}, compare_fields=['category'],
              rules=[dict(name='delta', metric='amount', measure='delta', max=1)],
              report={'evidence_exports': [{'kind': 'changed'}]})
    out = tmp_path / 'out'
    data = investigate(path, out)
    assert data['schema_checks']['status'] == 'passed'
    assert data['summary']['delta'] == Decimal('2.00')
    assert data['summary']['matched_rows'] == 1
    assert data['field_changes']['changed_rows'] == 1
    assert data['rule_checks']['status'] == 'failed'
    assert data['dimensions'][0]['columns'] == ['category']
    assert list(out.glob('*.csv'))
    manifest = json.loads((out / 'manifest.json').read_text(encoding='utf-8'))
    profile = manifest['inputs']['current']
    assert profile['column_mapping'] == mapping
    assert profile['source_schema'] == {'customerId': 'INTEGER', 'netAmount': 'DECIMAL(18,2)', 'region': 'VARCHAR'}
    assert profile['schema'] == manifest['inputs']['reference']['schema']
    with duckdb.connect() as con:
        con.execute((out / 'analysis.sql').read_text(encoding='utf-8'))
        assert con.execute('SELECT id, amount, category FROM current').fetchone() == (1, Decimal('12.00'), 'b')


def test_csv_types_use_source_names_and_both_sides_can_map(tmp_path):
    path = csv_config(tmp_path, 'oldId,total,group\n001,1.20,a\n',
                      'newId,value,category\n001,1.21,a\n',
                      {'reference': {'header': True, 'types': {'oldId': 'VARCHAR', 'total': 'DECIMAL(18,2)'}},
                       'current': {'header': True, 'types': {'newId': 'VARCHAR', 'value': 'DECIMAL(18,2)'}}})
    configure(path, column_mapping={'reference': {'oldId': 'id', 'total': 'amount', 'group': 'category'},
                                    'current': {'newId': 'id', 'value': 'amount'}})
    out = tmp_path / 'out'
    data = investigate(path, out)
    assert data['summary']['delta'] == Decimal('0.01')
    assert data['summary']['matched_rows'] == 1
    with duckdb.connect() as con:
        con.execute((out / 'analysis.sql').read_text(encoding='utf-8'))
        assert con.execute('SELECT id FROM current').fetchone() == ('001',)


@pytest.mark.parametrize('mapping', [None, [], {'other': {}}, {'current': None},
    {'current': {'id': None}}, {'current': {'id': ''}}, {'current': {'': 'id'}},
    {'current': {'id': 'a\x00b'}}, {'current': {'id': 'same', 'amount': 'SAME'}}])
def test_invalid_configuration(tmp_path, mapping):
    path = setup(tmp_path, [], [], column_mapping=mapping)
    with pytest.raises(InvestigationError):
        load_config(path)


@pytest.mark.parametrize('mapping, message', [
    ({'missing': 'id'}, 'unknown source'), ({'ID': 'id'}, 'unknown source'),
    ({'id': 'amount'}, 'collision'), ({'id': 'AMOUNT'}, 'collision'),
])
def test_bad_mapping_preserves_previous_bundle(tmp_path, mapping, message):
    path = setup(tmp_path, [(1, 10, 'a')], [(1, 12, 'a')])
    out = tmp_path / 'out'
    investigate(path, out)
    before = {p.name: p.read_bytes() for p in out.iterdir()}
    configure(path, column_mapping={'current': mapping})
    with pytest.raises(InvestigationError, match=message):
        investigate(path, out, overwrite=True)
    assert {p.name: p.read_bytes() for p in out.iterdir()} == before


@pytest.mark.parametrize('mapping', [
    {'id': 'amount', 'amount': 'id'}, {'id': 'ID'},
    {'id': 'quoted"name', 'amount': "amount'; DROP TABLE reference; --"},
])
def test_simultaneous_aliases_and_safe_identifiers(tmp_path, mapping):
    path = setup(tmp_path, [(1, 10, 'a')], [(1, 12, 'a')], column_mapping={'current': mapping})
    cfg = load_config(path)
    statements = []
    with duckdb.connect() as con:
        def execute(sql):
            con.execute(sql)
            statements.append(sql)
        profiles = load_snapshots(con, cfg, execute)
        cols = ', '.join(ident(mapping.get(n, n)) for n in ['id', 'amount', 'category'])
        assert con.execute(f'SELECT {cols} FROM current').fetchone() == (1, Decimal('12'), 'a')
        assert profiles['current']['schema'][mapping.get('amount', 'amount')] == 'DECIMAL(18,2)'
    with duckdb.connect() as con:
        for sql in statements:
            con.execute(sql)
        assert con.execute(f'SELECT {cols} FROM current').fetchone() == (1, Decimal('12'), 'a')


def test_mapping_does_not_coerce_types(tmp_path):
    path = setup(tmp_path, [(1, 10, 'a')], [(1, 12, 'a')],
                 column_mapping={'current': {'amount': 'id', 'id': 'amount'}})
    with pytest.raises(InvestigationError, match='Incompatible selected column types'):
        investigate(path, tmp_path / 'out')
