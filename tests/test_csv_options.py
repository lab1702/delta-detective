import json
from decimal import Decimal

import duckdb
import pytest

from delta_detective import investigate
from delta_detective.config import InvestigationError, load_config
from test_investigation import setup
from test_features import configure


def csv_config(tmp_path, reference, current=None, options=None):
    path = setup(tmp_path, [], [])
    for side, content in [('reference', reference), ('current', current or reference)]:
        (tmp_path / f'{side}.csv').write_text(content, encoding='utf-8')
    return configure(path, reference='reference.csv', current='current.csv',
                     csv=options or {})


def test_types_dialect_manifest_and_replay(tmp_path):
    opts = dict(header=True, delimiter=';', types={'id': 'VARCHAR', 'amount': 'DECIMAL(18,2)'})
    path = csv_config(tmp_path, 'id;amount;category\n001;12.30;a\n1;0.10;b\n',
                      'id,amount,category\n001,12.31,a\n1,0.10,b\n',
                      {'reference': opts, 'current': {**opts, 'delimiter': ','}})
    out = tmp_path / 'out'
    data = investigate(path, out)
    assert data['summary']['delta'] == Decimal('0.01')
    assert data['summary']['matched_rows'] == 2
    manifest = json.loads((out / 'manifest.json').read_text())
    parsing = manifest['inputs']['reference']['parsing']
    assert parsing['overrides'] == opts
    assert parsing['effective']['columns']['amount'] == 'DECIMAL(18,2)'
    assert parsing['effective']['delim'] == ';'
    with duckdb.connect() as con:
        con.execute((out / 'analysis.sql').read_text(encoding='utf-8'))
        assert con.execute('SELECT id, amount FROM reference ORDER BY id').fetchall() == [
            ('001', Decimal('12.30')), ('1', Decimal('0.10'))]


def test_dates_nulls_quotes_and_headerless(tmp_path):
    opts = dict(header=False, delimiter='|', quote="'", escape="'", nullstr='NA',
                dateformat='%d/%m/%Y', timestampformat='%d/%m/%Y %H:%M:%S',
                types={'column0': 'VARCHAR', 'column1': 'DECIMAL(18,2)',
                       'column2': 'VARCHAR', 'column3': 'DATE', 'column4': 'TIMESTAMP'})
    path = csv_config(tmp_path,
        "001|1.20|'a|b'|31/12/2025|31/12/2025 12:34:56\n002|2.30|NA|01/01/2026|01/01/2026 01:02:03\n",
        options={'reference': opts, 'current': opts})
    configure(path, key=['column0'], dimensions=['column2'],
              metric=dict(name='amount', aggregate='sum', column='column1', null_policy='error'))
    out = tmp_path / 'out'
    investigate(path, out)
    with duckdb.connect() as con:
        con.execute((out / 'analysis.sql').read_text(encoding='utf-8'))
        assert con.execute('SELECT column2 FROM reference ORDER BY column0').fetchall() == [('a|b',), (None,)]
        assert str(con.execute('SELECT min(column3), min(column4) FROM reference').fetchone()[0]) == '2025-12-31'


@pytest.mark.parametrize('options', [
    {'header': 'true'}, {'types': {}}, {'types': {'id': None}},
    {'types': {'id': 'not_a_type'}}, {'delimiter': ''}, {'delimiter': '\n'},
    {'quote': 'xx'}, {'nullstr': []}, {'dateformat': ''}, {'ignore_errors': True},
])
def test_invalid_options(tmp_path, options):
    path = csv_config(tmp_path, 'id,amount,category\n1,2,a\n', options={'reference': options})
    with pytest.raises(InvestigationError):
        load_config(path)


def test_parquet_options_rejected(tmp_path):
    path = setup(tmp_path, [], [], csv={'reference': {'header': True}})
    with pytest.raises(InvestigationError, match='requires a CSV'):
        load_config(path)


@pytest.mark.parametrize('types', [{'missing': 'VARCHAR'}, {'amount': 'INTEGER'}])
def test_unknown_columns_and_bad_values_fail_without_publication(tmp_path, types):
    path = csv_config(tmp_path, 'id,amount,category\n1,not-numeric,a\n',
                      options={'reference': {'header': True, 'types': types}})
    with pytest.raises(InvestigationError):
        investigate(path, tmp_path / 'out')
    assert not (tmp_path / 'out').exists()


def test_header_only_typed_csv(tmp_path):
    opts = {'header': True, 'types': {'id': 'VARCHAR', 'amount': 'DECIMAL(18,2)', 'category': 'VARCHAR'}}
    path = csv_config(tmp_path, 'id,amount,category\n',
                      'id,amount,category\n001,1.20,a\n',
                      {'reference': opts, 'current': opts})
    data = investigate(path, tmp_path / 'out')
    assert data['summary']['added_rows'] == 1
    assert data['summary']['delta'] == Decimal('1.20')


def test_custom_null_marker_preserves_empty_text(tmp_path):
    opts = {'header': True, 'nullstr': 'NA', 'types': {'category': 'VARCHAR'}}
    path = csv_config(tmp_path, 'id,amount,category\n1,2,\n2,3,NA\n',
                      options={'reference': opts, 'current': opts})
    out = tmp_path / 'out'
    investigate(path, out)
    with duckdb.connect() as con:
        con.execute((out / 'analysis.sql').read_text(encoding='utf-8'))
        assert con.execute('SELECT category FROM reference ORDER BY id').fetchall() == [('',), (None,)]


def test_overrides_do_not_allow_ragged_rows(tmp_path):
    opts = {'header': True, 'delimiter': ',', 'types': {'id': 'VARCHAR'}}
    path = csv_config(tmp_path, 'id,amount,category\n001,2,a\n002,3\n',
                      options={'reference': opts, 'current': opts})
    with pytest.raises(InvestigationError):
        investigate(path, tmp_path / 'out')
    assert not (tmp_path / 'out').exists()
