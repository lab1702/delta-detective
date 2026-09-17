import json
import subprocess
import sys

import duckdb
import pytest

from delta_detective import investigate
from delta_detective.config import InvestigationError, load_config
from test_features import configure
from test_investigation import setup


def test_contract_passes_and_normalizes_aliases(tmp_path):
    path = setup(tmp_path, [(1, 10, 'a')], [(1, 9, 'b')])
    configure(path, schema={'columns': {'id': 'int', 'amount': 'decimal(18, 2)', 'category': 'text'}, 'allow_extra_columns': False})
    cfg = load_config(path)
    assert cfg['schema']['columns'] == {'id': 'INTEGER', 'amount': 'DECIMAL(18,2)', 'category': 'VARCHAR'}
    out = tmp_path/'out'
    data = investigate(path, out)
    assert data['schema_checks']['status'] == 'passed'
    assert len(data['schema_checks']['results']) == 6
    assert data['summary']['delta'] == -1
    manifest = json.loads((out/'manifest.json').read_text())
    assert manifest['schema_checks'] == data['schema_checks']


def test_shared_type_drift_creates_schema_only_bundle(tmp_path):
    path = setup(tmp_path, [(1, 10, 'a')], [(1, 9, 'b')])
    configure(path, schema={'columns': {'id': 'VARCHAR'}}, report={'include_raw_rows': True})
    out = tmp_path/'out'
    result = subprocess.run([sys.executable, '-m', 'delta_detective.cli', 'investigate', str(path), '--out', str(out)], capture_output=True, text=True)
    assert result.returncode == 4, result.stderr
    assert 'type_mismatch' in result.stdout and 'Comparison not run' in result.stdout
    data = json.loads((out/'findings.json').read_text())
    assert data['execution_status'] == 'schema_contract_failed'
    assert data['metrics'] == [] and data['summary'] is None
    assert data['rule_checks']['status'] == 'not_evaluated'
    assert len(data['schema_checks']['results']) == 2
    assert all(r['issue'] == 'type_mismatch' for r in data['schema_checks']['results'])
    assert not list(out.glob('*.csv'))
    assert 'Comparison not run' in (out/'report.html').read_text()
    with duckdb.connect() as con:
        con.execute((out/'analysis.sql').read_text())
        assert con.execute('SELECT count(*) FROM main.reference').fetchone() == (1,)
        assert con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name='joined'").fetchone() == (0,)


def test_missing_selected_column_reports_before_comparison(tmp_path):
    path = setup(tmp_path, [], [])
    configure(path, key=['missing'], schema={'columns': {'missing': None}})
    data = investigate(path, tmp_path/'out')
    assert all(r['issue'] == 'missing_column' for r in data['schema_checks']['results'])
    assert data['execution_status'] == 'schema_contract_failed'


def test_presence_only_and_extra_columns(tmp_path):
    path = setup(tmp_path, [], [])
    configure(path, schema={'columns': {'id': None}})
    assert investigate(path, tmp_path/'allow')['schema_checks']['status'] == 'passed'
    configure(path, schema={'columns': {'id': None}, 'allow_extra_columns': False})
    checks = investigate(path, tmp_path/'strict')['schema_checks']['results']
    assert {(r['side'], r['column']) for r in checks if r['issue'] == 'unexpected_column'} == {
        (side, column) for side in ('reference', 'current') for column in ('amount', 'category')}


@pytest.mark.parametrize('schema', [None, {}, {'columns': {}}, {'columns': []},
    {'columns': {'id': True}}, {'columns': {'id': 'INVALID TYPE'}}, {'columns': {'id': ''}},
    {'columns': {'id': 'INTEGER'}, 'allow_extra_columns': 'false'},
    {'columns': {'id': 'INTEGER'}, 'unexpected': True}, {'columns': {'': 'INTEGER'}}])
def test_invalid_contract_preserves_previous_bundle(tmp_path, schema):
    path = setup(tmp_path, [], [])
    out = tmp_path/'out'
    investigate(path, out)
    before = {p.name: p.read_bytes() for p in out.iterdir()}
    configure(path, schema=schema)
    with pytest.raises(InvestigationError):
        investigate(path, out, overwrite=True)
    assert {p.name: p.read_bytes() for p in out.iterdir()} == before


def test_contract_failure_report_is_escaped(tmp_path):
    path = setup(tmp_path, [], [])
    configure(path, schema={'columns': {'<script>': None}})
    out = tmp_path/'out'
    investigate(path, out)
    html = (out/'report.html').read_text()
    assert '<script>' not in html and '&lt;script&gt;' in html


def test_csv_identifier_contract_detects_inference(tmp_path):
    path = setup(tmp_path, [], [])
    for side in ('reference', 'current'):
        (tmp_path/f'{side}.csv').write_text('id,amount,category\n1,10,a\n')
    configure(path, reference='reference.csv', current='current.csv', schema={'columns': {'id': 'VARCHAR'}})
    data = investigate(path, tmp_path/'out')
    assert data['schema_checks']['status'] == 'failed'
    assert all(r['observed_type'] == 'BIGINT' for r in data['schema_checks']['results'])


def test_contract_does_not_replace_key_validation(tmp_path):
    path = setup(tmp_path, [(1, 10, 'a'), (1, 10, 'a')], [])
    configure(path, schema={'columns': {'id': 'INTEGER'}})
    with pytest.raises(InvestigationError, match='duplicate'):
        investigate(path, tmp_path/'out')
    assert not (tmp_path/'out').exists()
