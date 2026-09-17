import re
from datetime import date

import pytest

from delta_detective import investigate
from delta_detective.config import load_config, InvestigationError
from delta_detective.wizard import init_config
from test_investigation import setup


def run(tmp_path, answers):
    messages = []
    pending = iter(answers)
    def ask(prompt):
        answer = next(pending)
        if isinstance(answer, tuple):
            prefix, label = answer
            assert prompt.startswith(prefix), prompt
            for message in reversed(messages):
                match = re.fullmatch(r'  (\d+)\. (.*)', message)
                if match and match[2] == label:
                    return match[1]
            raise AssertionError(f'No menu option {label}')
        return answer
    path = init_config(tmp_path/'reference.parquet', tmp_path/'current.parquet', tmp_path/'new.yaml', ask=ask, tell=messages.append)
    with pytest.raises(StopIteration):
        next(pending)
    return path, messages


def test_full_advanced_wizard_roundtrip(tmp_path):
    setup(tmp_path, [(1, 10, 'West', 'open', date(2026, 1, 1))],
          [(1, 8, 'South', 'closed', date(2026, 1, 3))],
          schema='id INTEGER, amount INTEGER, category VARCHAR, status VARCHAR, delivery DATE')
    path, messages = run(tmp_path, [
        '1', '1', '', '2', '',  # Basic key, count metric, category dimension.
        'yes', 'yes', '1,2,3,4,5', '', '', '', '', '', '', 'yes',  # Strict inferred contract.
        '3,4', '7', '2',  # Compare status/date, export changed fields with limit 2.
        'status rule', ('Rule target', 'Compared field'), '1', ('Measure number', 'percent_changed'), '', '2', 'yes', '1',
        'retention', ('Rule target', 'Segment metric'), '1', '1', 'yes', '"West"', ('Measure number', 'removed_percent'), '', '5', 'yes', '',
        'later', ('Rule target', 'Compared field'), '2', ('Measure number', 'increased_rows'), '', '0', '',
        'overall', ('Rule target', 'Overall metric'), '1', ('Measure number', 'current_rows'), '1', '1', '',
        '',
    ])
    cfg = load_config(path)
    assert cfg['schema']['allow_extra_columns'] is False
    assert cfg['compare_fields'] == ['status', 'delivery']
    assert cfg['rules'][0]['field'] == 'status' and cfg['rules'][0]['export']['limit'] == 1
    assert cfg['rules'][1]['where'] == {'category': 'West'}
    assert cfg['rules'][1]['export']['limit'] == 100
    assert cfg['report']['include_raw_rows'] is False
    data = investigate(path, tmp_path/'out')
    assert [r['status'] for r in data['rule_checks']['results']] == ['failed', 'failed', 'failed', 'passed']
    assert (tmp_path/'out/rule_0_rows.csv').exists()
    assert (tmp_path/'out/field_changed_rows.csv').exists()
    assert any('Raw exports are enabled as selected' in m for m in messages)
    assert not any('secret' in m for m in messages)


def test_advanced_defaults_keep_exports_disabled(tmp_path):
    setup(tmp_path, [(1, 10, 'private-value')], [(1, 10, 'private-value')])
    path, messages = run(tmp_path, ['1', '1', '', '', '', 'y', '', '', '', ''])
    cfg = load_config(path)
    assert 'schema' not in cfg and cfg['compare_fields'] == []
    assert cfg['report'] == {'include_raw_rows': False, 'evidence_exports': []}
    assert not any('private-value' in m for m in messages)


def test_contract_overrides_presence_and_missing_column(tmp_path):
    setup(tmp_path, [], [])
    path, messages = run(tmp_path, ['1', '1', '', '', '', 'y', 'y', '1,2',
                                   'nonsense type', 'VARCHAR', '*', 'required_extra', '*', '', '', '', '', ''])
    cfg = load_config(path)
    assert cfg['schema']['columns'] == {'id': 'VARCHAR', 'amount': None, 'required_extra': None}
    assert any('exit 4' in m for m in messages)
    assert investigate(path, tmp_path/'out')['execution_status'] == 'schema_contract_failed'


def test_selector_type_retry_null_and_export_limit_retry(tmp_path):
    setup(tmp_path, [(1, 10, None)], [(1, 9, None)])
    path, messages = run(tmp_path, ['1', '1', '', '2', '', 'y', '', '', '',
                                   'segment', ('Rule target', 'Segment metric'), '1', '1', 'y',
                                   '123', 'null', ('Measure number', 'current_rows'), '2', '', 'y', '0', '-1', '1.5', '2', ''])
    cfg = load_config(path)
    assert cfg['rules'][0]['where'] == {'category': None}
    assert cfg['rules'][0]['export']['limit'] == 2
    assert any('matching the column type' in m for m in messages)
    assert investigate(path, tmp_path/'out')['rule_checks']['status'] == 'failed'


def test_field_menu_excludes_invalid_direction_measures(tmp_path):
    setup(tmp_path, [(1, 10, 'a')], [(1, 10, 'b')])
    path, messages = run(tmp_path, ['1', '1', '', '', '', 'y', '', '2', '',
                                   'field', ('Rule target', 'Compared field'), '1', ('Measure number', 'became_blank'), '', '0', '', ''])
    assert not any(re.fullmatch(r'  \d+\. (increased_rows|percent_increased)', m) for m in messages)
    assert investigate(path, tmp_path/'out')['rule_checks']['status'] == 'passed'


def test_advanced_cancellation_after_choices_writes_nothing(tmp_path):
    setup(tmp_path, [], [])
    answers = iter(['1', '1', '', '', '', 'y'])
    def ask(prompt):
        try:
            return next(answers)
        except StopIteration:
            raise EOFError
    with pytest.raises(InvestigationError, match='cancelled'):
        init_config(tmp_path/'reference.parquet', tmp_path/'current.parquet', tmp_path/'new.yaml', ask=ask, tell=lambda s: None)
    assert not (tmp_path/'new.yaml').exists()
