import subprocess
import sys

import pytest
import duckdb

from delta_detective import investigate
from delta_detective.config import InvestigationError, load_config
from delta_detective.wizard import init_config
from delta_detective.loading import literal
from test_investigation import setup


def run_wizard(tmp_path, answers, **kwargs):
    answers = iter(answers)
    messages = []
    path = init_config(tmp_path/'reference.parquet', tmp_path/'current.parquet', tmp_path/'new.yaml',
                       ask=lambda prompt: '' if prompt.startswith('Configure schema,') else next(answers), tell=messages.append, **kwargs)
    return path, messages


def test_wizard_multiple_metrics_group_and_rule(tmp_path):
    setup(tmp_path, [(1, 10, 'a')], [(1, 8, 'b')])
    path, messages = run_wizard(tmp_path, ['1', '1,2', '', '', '2', '1,2', '', 'Revenue check', '2', '1', '', '2', ''])
    cfg = load_config(path)
    assert cfg['key'] == ['id']
    assert cfg['metrics'] == [dict(name='rows', aggregate='count'),
                              dict(name='amount', aggregate='sum', column='amount', null_policy='error')]
    assert cfg['dimensions'] == ['category']
    assert cfg['dimension_groups'] == [['amount', 'category']]
    assert cfg['rules'] == [dict(name='Revenue check', metric='amount', measure='abs_delta', max='2')]
    assert cfg['report']['include_raw_rows'] is False
    data = investigate(path, tmp_path/'out')
    assert data['rule_checks']['status'] == 'passed'
    assert any('unique and non-null' in m for m in messages)
    assert not list(tmp_path.glob('.delta-init-*'))


def test_composite_key_retry_and_numeric_looking_identifier(tmp_path):
    setup(tmp_path, [(1, '01', 10, 'a'), (1, '02', 20, 'a')],
          [(1, '01', 10, 'a'), (2, '01', 20, 'b')],
          schema='id INTEGER, sub VARCHAR, amount INTEGER, category VARCHAR', key=['id', 'sub'])
    path, messages = run_wizard(tmp_path, ['1', '1,2', '1', '', '', '', ''])
    assert load_config(path)['key'] == ['id', 'sub']
    assert any('duplicate tuples' in m for m in messages)
    assert any('not a valid single key' in m for m in messages)
    assert investigate(path, tmp_path/'out')['summary']['matched_rows'] == 1


def test_bad_menu_choices_and_rules_retry(tmp_path):
    setup(tmp_path, [(1, 10, 'a')], [(1, 8, 'b')])
    path, messages = run_wizard(tmp_path, ['', 'no', '0', '1,1', '1', '2', '', '', '',
                                         'bad', '1', '1', '3', '2',
                                         'good', '1', '1', '', '0.000000000000000000001', ''])
    cfg = load_config(path)
    assert len(cfg['rules']) == 1
    assert cfg['rules'][0]['max'] == '0.000000000000000000001'
    assert any('min <= max' in m for m in messages)


def test_excludes_null_metrics(tmp_path):
    setup(tmp_path, [(1, None, 'a')], [(1, 2, 'b')])
    path, messages = run_wizard(tmp_path, ['1', '1', '', '', '', ''])
    assert load_config(path)['metrics'] == [dict(name='rows', aggregate='count')]
    assert any('null or nonfinite' in m for m in messages)


@pytest.mark.parametrize('error', [EOFError, KeyboardInterrupt])
def test_cancellation_writes_nothing(tmp_path, error):
    setup(tmp_path, [(1, 10, 'a')], [(1, 8, 'b')])
    def cancel(prompt):
        raise error
    with pytest.raises(InvestigationError, match='cancelled'):
        init_config(tmp_path/'reference.parquet', tmp_path/'current.parquet', tmp_path/'new.yaml', ask=cancel, tell=lambda s: None)
    assert not (tmp_path/'new.yaml').exists()


def test_incompatible_columns_are_shown_but_not_selectable(tmp_path):
    setup(tmp_path, [(1, 10, 'a')], [(1, 8, 'b')])
    with duckdb.connect() as con:
        con.execute(f"COPY (SELECT 1 AS id, 'bad' AS amount, 'b' AS category) TO {literal(tmp_path/'current.parquet')} (FORMAT PARQUET)")
    path, messages = run_wizard(tmp_path, ['1', '1', '', '1', ''])
    assert load_config(path)['dimensions'] == ['category']
    assert any('DECIMAL(18,2) / VARCHAR' in m for m in messages)
    assert not any('SUM(' in m for m in messages)


def test_malformed_csv_does_not_expose_values(tmp_path):
    (tmp_path/'reference.csv').write_text('id,amount\n1,10\n')
    (tmp_path/'current.csv').write_text('id,amount\n1,10\n2,20,SECRET_VALUE\n')
    with pytest.raises(InvestigationError) as exc:
        init_config(tmp_path/'reference.csv', tmp_path/'current.csv', tmp_path/'new.yaml', tell=lambda s: None)
    assert 'SECRET_VALUE' not in str(exc.value)
    assert not (tmp_path/'new.yaml').exists()
    assert not list(tmp_path.glob('.delta-init-*'))


def test_existing_output_and_source_are_protected(tmp_path):
    setup(tmp_path, [], [])
    for target in [tmp_path/'config.yaml', tmp_path/'reference.parquet']:
        before = target.read_bytes()
        with pytest.raises(InvestigationError, match='already exists'):
            init_config(tmp_path/'reference.parquet', tmp_path/'current.parquet', target)
        assert target.read_bytes() == before


def test_concurrent_output_creation_is_not_overwritten(tmp_path):
    setup(tmp_path, [], [])
    target = tmp_path/'new.yaml'
    answers = iter(['1', '1', '', '', '', ''])
    def ask(prompt):
        if prompt.startswith('Configure schema,'):
            return ''
        if not target.exists():
            target.write_text('another writer')
        return next(answers)
    with pytest.raises(FileExistsError):
        init_config(tmp_path/'reference.parquet', tmp_path/'current.parquet', target, ask=ask, tell=lambda s: None)
    assert target.read_text() == 'another writer'
    assert not list(tmp_path.glob('.delta-init-*'))


def test_cli_csv_roundtrip_and_eof(tmp_path):
    for side, amount in [('reference', 10), ('current', 8)]:
        (tmp_path/f'{side}.csv').write_text(f'id,amount,category\n1,{amount},a\n')
    target = tmp_path/'nested'/'comparison.yaml'
    cmd = [sys.executable, '-m', 'delta_detective.cli', 'init', str(tmp_path/'reference.csv'),
           str(tmp_path/'current.csv'), '--out', str(target)]
    cancelled = subprocess.run(cmd, input='', capture_output=True, text=True)
    assert cancelled.returncode == 2 and 'cancelled' in cancelled.stderr and 'Traceback' not in cancelled.stderr
    result = subprocess.run(cmd, input='1\n2\n\n2\n\n\n\n', capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert investigate(target, tmp_path/'out')['summary']['delta'] == -2


def test_overflow_does_not_publish_configuration(tmp_path):
    setup(tmp_path, [], [(1, '9'*38, 'a'), (2, '9'*38, 'b')],
          schema='id INTEGER, amount DECIMAL(38,0), category VARCHAR')
    with pytest.raises(InvestigationError, match='overflow'):
        run_wizard(tmp_path, ['1', '2', '', '', '', ''])
    assert not (tmp_path/'new.yaml').exists()
    assert not list(tmp_path.glob('.delta-init-*'))


def test_input_mutation_is_detected(tmp_path):
    setup(tmp_path, [], [])
    answers = iter(['1', '1', '', '', '', ''])
    def ask(prompt):
        if prompt.startswith('Configure schema,'):
            return ''
        if prompt.startswith('Rule name'):
            with (tmp_path/'current.parquet').open('ab') as stream:
                stream.write(b'changed')
        return next(answers)
    with pytest.raises(InvestigationError, match='Input changed'):
        init_config(tmp_path/'reference.parquet', tmp_path/'current.parquet', tmp_path/'new.yaml', ask=ask, tell=lambda s: None)
    assert not (tmp_path/'new.yaml').exists()
