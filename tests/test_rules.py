import json
import subprocess
import sys
from decimal import Decimal

import pytest

from delta_detective import investigate
from delta_detective.config import InvestigationError
from test_features import configure
from test_investigation import setup


def rule(measure, **bounds):
    return dict(name=measure, metric='amount', measure=measure, **bounds)


def test_rules_report_manifest_and_inclusive_bounds(tmp_path):
    path = setup(tmp_path, [(1, 60, 'a'), (2, 40, 'b')], [(1, 70, 'a')])
    configure(path, rules=[rule('abs_percent_change', max=30), rule('delta', min=-30, max=-30),
                           rule('removed_percent', max=1), rule('current_rows', min=1, max=1),
                           dict(rule('current_total', max=60), name='<script>bad</script>')])
    out = tmp_path/'out'
    data = investigate(path, out)
    checks = data['rule_checks']
    assert checks['status'] == 'failed'
    assert [r['status'] for r in checks['results']] == ['passed', 'passed', 'failed', 'passed', 'failed']
    assert [r['observed'] for r in checks['results']] == [30, -30, 50, 1, 70]
    assert data['summary']['status'] == 'passed'
    manifest = json.loads((out/'manifest.json').read_text(encoding='utf-8'))
    findings = json.loads((out/'findings.json').read_text(encoding='utf-8'))
    assert manifest['execution_status'] == 'success'
    assert manifest['rule_checks'] == findings['rule_checks']
    html = (out/'report.html').read_text(encoding='utf-8')
    assert 'Threshold rules: failed' in html
    assert '<script>' not in html and '&lt;script&gt;bad&lt;/script&gt;' in html


@pytest.mark.parametrize('reference,current', [(0, 0), (0, 10), (-100, -110), (100, 110)])
def test_percent_signs_and_zero_reference(tmp_path, reference, current):
    path = setup(tmp_path, [(1, reference, 'a')], [(1, current, 'a')])
    configure(path, rules=[rule('abs_percent_change', max=10), rule('percent_change', min=0, max=10)])
    results = investigate(path, tmp_path/'out')['rule_checks']['results']
    assert [r['status'] for r in results] == (['undefined']*2 if reference == 0 else ['passed']*2)
    assert [r['observed'] for r in results] == ([None]*2 if reference == 0 else [10]*2)


def test_empty_reference_removed_percent_is_undefined(tmp_path):
    path = setup(tmp_path, [], [(1, 10, 'a')])
    configure(path, rules=[rule('removed_percent', max=1)])
    checks = investigate(path, tmp_path/'out')['rule_checks']
    assert checks['status'] == 'failed'
    assert checks['results'][0]['status'] == 'undefined'
    assert checks['results'][0]['observed'] is None
    assert 'row count' in checks['results'][0]['reason']


def test_multiple_metrics_and_exact_large_bounds(tmp_path):
    path = setup(tmp_path, [(1, '99999999999999999999999999999.01', 'a')],
                 [(1, '99999999999999999999999999999.02', 'a')],
                 schema='id INTEGER, amount DECIMAL(38,2), category VARCHAR')
    configure(path, metrics=[dict(name='amount', aggregate='sum', column='amount', null_policy='error'),
                             dict(name='rows', aggregate='count')],
              rules=[rule('abs_delta', max='0.01'), rule('current_total', max='99999999999999999999999999999.01'),
                     dict(name='row count', metric='rows', measure='current_total', min=1, max=1)])
    checks = investigate(path, tmp_path/'out')['rule_checks']
    assert [r['status'] for r in checks['results']] == ['passed', 'failed', 'passed']
    assert checks['results'][0]['observed'] == Decimal('.01')
    assert checks['results'][2]['evidence'] == 'analysis.sql: metric_1.reconciliation'


@pytest.mark.parametrize('rules', [None, {}, [None], [rule('bogus', max=1)],
    [rule('delta')], [rule('delta', min=2, max=1)], [rule('delta', max=True)],
    [rule('delta', max=None)], [rule('delta', max='NaN')], [rule('delta', max=float('inf'))],
    [rule('delta', max='nonsense')], [rule('delta', max=[])],
    [dict(rule('delta', max=1), metric='unknown')], [dict(rule('delta', max=1), name=' ')],
    [dict(rule('delta', max=1), unexpected=True)], [rule('delta', max=1)]*2])
def test_invalid_rules_preserve_existing_bundle(tmp_path, rules):
    path = setup(tmp_path, [(1, 10, 'a')], [(1, 9, 'a')])
    out = tmp_path/'out'
    investigate(path, out)
    before = {p.name: p.read_bytes() for p in out.iterdir()}
    configure(path, rules=rules)
    with pytest.raises(InvestigationError):
        investigate(path, out, overwrite=True)
    assert {p.name: p.read_bytes() for p in out.iterdir()} == before


@pytest.mark.parametrize('rules,flag,expected', [
    ([], True, 0), ([rule('abs_delta', max=0)], False, 0),
    ([rule('abs_delta', max=0)], True, 3), ([rule('abs_delta', max=1)], True, 0),
    ([rule('abs_percent_change', max=100)], True, 3),
])
def test_cli_exit_codes_always_write_completed_bundle(tmp_path, rules, flag, expected):
    path = setup(tmp_path, [(1, 0, 'a')], [(1, 1, 'a')])
    configure(path, rules=rules)
    out = tmp_path/'out'
    cmd = [sys.executable, '-m', 'delta_detective.cli', 'investigate', str(path), '--out', str(out)]
    if flag:
        cmd.append('--fail-on-rule-violation')
    result = subprocess.run(cmd, capture_output=True, text=True)
    assert result.returncode == expected, result.stderr
    assert not result.stderr
    assert {p.name for p in out.iterdir()} == {'report.html', 'findings.json', 'manifest.json', 'analysis.sql'}
    assert 'Threshold rules:' in result.stdout


def test_execution_error_still_returns_two(tmp_path):
    path = setup(tmp_path, [(1, 1, 'a')], [(1, None, 'a')])
    configure(path, rules=[rule('abs_delta', max=1)])
    result = subprocess.run([sys.executable, '-m', 'delta_detective.cli', 'investigate', str(path),
                             '--out', str(tmp_path/'out'), '--fail-on-rule-violation'], capture_output=True, text=True)
    assert result.returncode == 2
    assert not (tmp_path/'out').exists()


def test_float_policy_has_no_reconciliation_tolerance(tmp_path):
    path = setup(tmp_path, [(1, 1., 'a')], [(1, 1.0000000001, 'a')],
                 schema='id INTEGER, amount DOUBLE, category VARCHAR')
    configure(path, rules=[rule('abs_delta', max=0), rule('added_rows', max=0), rule('removed_rows', max=0)])
    checks = investigate(path, tmp_path/'out')['rule_checks']
    assert checks['results'][0]['status'] == 'failed'
    assert checks['results'][0]['exact'] is False
    assert checks['results'][1]['exact'] is True
    assert checks['results'][1]['status'] == checks['results'][2]['status'] == 'passed'
