import json
import subprocess
import sys

import duckdb
import pytest

from delta_detective import investigate
from delta_detective.validation import validate
from delta_detective.config import InvestigationError
from test_features import configure
from test_investigation import setup
from test_csv_options import csv_config


def check(data, side, name):
    return next(c for c in data['checks'] if c['side'] == side and c['check'] == name)


def test_multiple_diagnostics_privacy_and_replay(tmp_path):
    rows = [('secret-key', None, 'private-category'), ('secret-key', float('inf'), 'private-category'),
            (None, 2, 'private-category'), (None, 3, 'private-category')]
    path = setup(tmp_path, rows, rows, schema='id VARCHAR, amount DOUBLE, category VARCHAR')
    out = tmp_path / 'validation'
    data = validate(path, out)
    assert data['status'] == 'failed'
    assert check(data, 'reference', 'null_key')['details']['affected_rows'] == 2
    assert check(data, 'reference', 'null_metric')['details']['affected_rows'] == 1
    assert check(data, 'reference', 'nonfinite_metric')['details']['affected_rows'] == 1
    details = check(data, 'reference', 'duplicate_key')['details']
    assert (details['duplicate_groups'], details['affected_rows'], details['excess_rows']) == (2, 4, 2)
    assert not list(out.glob('*.csv'))
    for file in out.iterdir():
        text = file.read_text(encoding='utf-8')
        assert 'secret-key' not in text and 'private-category' not in text
    assert json.loads((out / 'validation.json').read_text()) == data
    with duckdb.connect() as con:
        con.execute((out / 'analysis.sql').read_text())
        assert con.execute('SELECT sum(validation_group_rows) FROM reference_duplicate_keys').fetchone() == (4,)
        assert con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name='joined'").fetchone() == (0,)


def test_exports_are_limited_and_only_selected_columns(tmp_path):
    rows = [(1, None, 'do-not-export'), (1, None, 'do-not-export'), (1, None, 'do-not-export')]
    path = setup(tmp_path, rows, rows)
    out = tmp_path / 'validation'
    data = validate(path, out, export_invalid_rows=True, limit=2)
    assert len(data['exports']) == 4
    with duckdb.connect() as con:
        for export in data['exports']:
            assert export['rows'] == 2 and export['total_rows'] == 3 and export['truncated']
            text = (out / export['file']).read_text()
            assert 'do-not-export' not in text
            assert len(con.execute('SELECT * FROM read_csv(?, header=true)', [str(out / export['file'])]).fetchall()) == 2


def test_mapping_csv_filters_and_no_rule_evaluation(tmp_path):
    path = csv_config(tmp_path, 'source_id,amount,category\n001,2,a\n002,,b\n002,3,b\n',
                      options={s: {'header': True, 'types': {'source_id': 'VARCHAR', 'amount': 'INTEGER'}} for s in ('reference', 'current')})
    configure(path, column_mapping={s: {'source_id': 'id'} for s in ('reference', 'current')},
              filters=[dict(column='category', operator='equals', value='a')],
              rules=[dict(name='would fail', metric='amount', measure='current_rows', max=0)],
              report={'include_raw_rows': True})
    data = validate(path, tmp_path / 'validation')
    assert data['status'] == 'passed' and data['exports'] == []
    assert data['filter_scope']['inputs']['reference']['included_rows'] == 1
    assert data['filter_scope']['inputs']['reference']['excluded_rows'] == 2


def test_missing_columns_and_bad_types_are_reported_together(tmp_path):
    path = setup(tmp_path, [(1, 'bad', 'a')], [(1, 'bad', 'a')],
                 schema='id INTEGER, amount VARCHAR, category VARCHAR')
    configure(path, key=['missing'], compare_fields=['category'], field_tolerances={'category': {'absolute': 1}})
    data = validate(path, tmp_path / 'validation')
    assert data['status'] == 'failed'
    failed = [c for c in data['checks'] if c['status'] == 'failed']
    assert any(c['columns'] == ['missing'] for c in failed)
    assert any(c['columns'] == ['amount'] for c in failed)
    assert any(c['columns'] == ['category'] for c in failed)


def test_schema_failure_and_filter_failure_are_explicit(tmp_path):
    path = setup(tmp_path, [], [])
    configure(path, schema={'columns': {'missing': None}})
    data = validate(path, tmp_path / 'schema')
    assert data['status'] == 'failed' and data['filter_scope']['status'] == 'not_evaluated'
    configure(path, schema={'columns': {'id': None}}, filters=[dict(column='absent', operator='is_null')])
    data = validate(path, tmp_path / 'filter')
    assert check(data, 'both', 'filter_configuration')['status'] == 'failed'


def test_overwrite_protection_and_failed_execution_preserves_output(tmp_path):
    path = setup(tmp_path, [(1, 1, 'a')], [(1, 2, 'a')])
    out = tmp_path / 'investigation'
    investigate(path, out)
    before = {p.name: p.read_bytes() for p in out.iterdir()}
    with pytest.raises(InvestigationError, match='separate'):
        validate(path, out, overwrite=True)
    assert {p.name: p.read_bytes() for p in out.iterdir()} == before
    target = tmp_path / 'validation'
    validate(path, target)
    validate(path, target, overwrite=True)
    before = {p.name: p.read_bytes() for p in target.iterdir()}
    (tmp_path / 'current.parquet').write_text('broken')
    with pytest.raises(InvestigationError, match='Could not load'):
        validate(path, target, overwrite=True)
    assert {p.name: p.read_bytes() for p in target.iterdir()} == before


def test_cli_exit_codes(tmp_path):
    path = setup(tmp_path, [(1, 1, 'a')], [(1, 2, 'a')])
    command = [sys.executable, '-m', 'delta_detective.cli', 'validate', str(path), '--out', str(tmp_path / 'validation')]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0 and 'Input validation: passed' in result.stdout
    configure(path, key=['missing'])
    result = subprocess.run(command + ['--overwrite'], capture_output=True, text=True)
    assert result.returncode == 4 and 'selected_column' in result.stdout
    result = subprocess.run(command + ['--overwrite', '--limit', '0'], capture_output=True, text=True)
    assert result.returncode == 2 and 'positive integer' in result.stderr


def test_composite_keys_and_internal_alias_collision(tmp_path):
    rows = [(1, 7, 1, 'a'), (1, 7, 2, 'a'), (1, 8, 3, 'b')]
    path = setup(tmp_path, rows, rows,
                 schema='id INTEGER, validation_group_rows INTEGER, amount INTEGER, category VARCHAR',
                 key=['id', 'validation_group_rows'])
    data = validate(path, tmp_path / 'validation', export_invalid_rows=True)
    assert check(data, 'current', 'duplicate_key')['details']['affected_rows'] == 2


def test_input_mutation_prevents_publication(tmp_path, monkeypatch):
    path = setup(tmp_path, [], [])
    import delta_detective.validation as module
    original = module.fingerprint
    calls = 0
    def fingerprint(source):
        nonlocal calls
        calls += 1
        return original(source) if calls <= 2 else 'changed'
    monkeypatch.setattr(module, 'fingerprint', fingerprint)
    with pytest.raises(InvestigationError, match='Input changed'):
        validate(path, tmp_path / 'validation')
    assert not (tmp_path / 'validation').exists()
