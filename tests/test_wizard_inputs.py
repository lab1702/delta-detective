from decimal import Decimal

import pytest

from delta_detective import investigate
from delta_detective.config import InvestigationError, load_config
from delta_detective.wizard import init_config
from test_investigation import setup


def run(tmp_path, answers, suffix='parquet'):
    pending = iter(answers)
    messages = []
    def ask(prompt):
        expected, answer = next(pending)
        assert prompt.startswith(expected), (prompt, expected)
        return answer
    path = init_config(tmp_path / f'reference.{suffix}', tmp_path / f'current.{suffix}',
                       tmp_path / 'new.yaml', ask=ask, tell=messages.append)
    assert list(pending) == []
    return path, messages


FINISH = [('Key column', '1'), ('Metric numbers', '2'), ('Metric name', ''),
          ('Dimension numbers', '2'), ('Combined group', ''), ('Configure schema,', ''), ('Rule name', '')]


def test_csv_mapping_filter_roundtrip(tmp_path):
    for side, col, amount in [('reference', 'oldId', '1.20'), ('current', 'newId', '1.21')]:
        (tmp_path / f'{side}.csv').write_text(
            f'{col};total;region\n001;{amount};East\n002;;West\n002;99;West\n', encoding='utf-8')
    answers = [('Configure input', 'yes')]
    for side, col in [('reference', 'oldId'), ('current', 'newId')]:
        answers.extend([(f'{side} CSV option', '2'), ('delimiter as', '";"'),
                        (f'{side} CSV option', '3'), ('Header present', 'true'),
                        (f'{side} CSV option', '1'), ('Source column', col), ('DuckDB type', 'VARCHAR'),
                        (f'{side} CSV option', '1'), ('Source column', 'total'), ('DuckDB type', 'DECIMAL(18,2)'),
                        (f'{side} CSV option', '')])
    for side, col in [('reference', 'oldId'), ('current', 'newId')]:
        answers.extend([(f'{side} columns', '1,2,3'), (f'Logical name for {col!r}', 'id'),
                        ('Logical name', 'amount'), ('Logical name', 'category')])
    answers += [('Filter column', '3'), ('Filter operator', '1'), ('Filter value', '"East"'),
                ('Filter column', '')] + FINISH
    path, messages = run(tmp_path, answers, 'csv')
    cfg = load_config(path)
    assert cfg['csv']['current']['types']['total'] == 'DECIMAL(18,2)'
    assert cfg['column_mapping']['current'] == {'newId': 'id', 'total': 'amount', 'region': 'category'}
    assert cfg['filters'] == [dict(column='category', operator='equals', value='East')]
    assert any('current preview: 1 included, 2 excluded, 3 total' in m for m in messages)
    assert not any('Sum unavailable' in m for m in messages)
    data = investigate(path, tmp_path / 'out')
    assert data['summary']['delta'] == Decimal('0.01')
    assert data['summary']['matched_rows'] == 1


def test_collision_and_filter_value_retry(tmp_path):
    setup(tmp_path, [(1, 1, 'a')], [(1, 2, 'a')])
    path, messages = run(tmp_path, [
        ('Configure input', 'y'), ('reference columns', '1'), ('Logical name', 'AMOUNT'),
        ('reference columns', ''), ('current columns', ''),
        ('Filter column', '2'), ('Filter operator', '4'),
        ('Filter value', 'true'), ('Filter value', '"0.999999999999999999"'), ('Filter column', ''),
    ] + FINISH)
    assert any('collide' in m for m in messages)
    assert any('Invalid filter' in m for m in messages)
    assert load_config(path)['filters'][0]['value'] == '0.999999999999999999'
    assert investigate(path, tmp_path / 'out')['summary']['matched_rows'] == 1


def test_empty_population_and_membership(tmp_path):
    setup(tmp_path, [(1, 1, 'a')], [(1, 2, 'a')])
    path, messages = run(tmp_path, [
        ('Configure input', 'y'), ('reference columns', ''), ('current columns', ''),
        ('Filter column', '3'), ('Filter operator', '2'), ('Filter value', '[]'),
        ('Filter value', '["absent", "also absent"]'), ('Filter column', ''),
    ] + FINISH)
    assert any('Both selected populations are empty' in m for m in messages)
    assert investigate(path, tmp_path / 'out')['summary']['current_rows'] == 0


def test_csv_setting_retry_and_no_unrequested_defaults(tmp_path):
    for side in ('reference', 'current'):
        (tmp_path / f'{side}.csv').write_text('id,amount,category\n001,1,a\n', encoding='utf-8')
    path, messages = run(tmp_path, [
        ('Configure input', 'y'), ('reference CSV option', '3'), ('Header present', '"true"'),
        ('reference CSV option', '2'), ('delimiter as', 'not JSON'),
        ('reference CSV option', '1'), ('Source column', 'id'), ('DuckDB type', 'notatype'),
        ('reference CSV option', ''), ('current CSV option', ''),
        ('reference columns', ''), ('current columns', ''), ('Filter column', ''),
    ] + FINISH, 'csv')
    cfg = load_config(path)
    assert 'csv' not in cfg and 'column_mapping' not in cfg and 'filters' not in cfg
    assert sum('Invalid CSV setting' in m for m in messages) == 3


@pytest.mark.parametrize('phase', ['reference CSV option', 'reference columns', 'Filter column'])
def test_cancellation_during_input_setup(tmp_path, phase):
    for side in ('reference', 'current'):
        (tmp_path / f'{side}.csv').write_text('id,amount,category\n1,1,a\n', encoding='utf-8')
    def ask(prompt):
        if prompt.startswith(phase):
            raise EOFError
        return 'yes' if prompt.startswith('Configure input') else ''
    with pytest.raises(InvestigationError, match='cancelled'):
        init_config(tmp_path / 'reference.csv', tmp_path / 'current.csv', tmp_path / 'new.yaml',
                    ask=ask, tell=lambda s: None)
    assert not (tmp_path / 'new.yaml').exists()
    assert not list(tmp_path.glob('.delta-init-*'))


def test_null_filter_and_simultaneous_mapping(tmp_path):
    setup(tmp_path, [(1, 1, None), (2, 2, 'a')], [(1, 2, None), (2, 3, 'a')],
          schema='id INTEGER, amount INTEGER, category VARCHAR')
    answers = [('Configure input', 'yes')]
    for side in ('reference', 'current'):
        answers += [(f'{side} columns', '1,2'), ('Logical name', 'amount'), ('Logical name', 'id')]
    answers += [('Filter column', '3'), ('Filter operator', '3'), ('Filter column', '')]
    answers += [('Key column', '2'), ('Metric numbers', '2'), ('Metric name', ''),
                ('Dimension numbers', ''), ('Combined group', ''), ('Configure schema,', ''), ('Rule name', '')]
    path, messages = run(tmp_path, answers)
    data = investigate(path, tmp_path / 'out')
    assert data['summary']['current_rows'] == 1
    assert data['summary']['added_rows'] == data['summary']['removed_rows'] == 1
