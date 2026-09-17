import json
import subprocess
import sys
from datetime import date

import pytest

from delta_detective import investigate
from delta_detective.config import InvestigationError
from test_features import configure
from test_investigation import setup


def rule(measure='percent_changed', **bounds):
    return dict(name=measure, field='status', measure=measure, **bounds)


def test_field_rules_counts_percentages_and_mixed_targets(tmp_path):
    path = setup(tmp_path, [(1, 10, 'a', 'open'), (2, 10, 'a', None), (3, 10, 'a', 'open')],
                 [(1, 10, 'a', 'closed'), (2, 10, 'a', ''), (3, 10, 'a', 'open'), (4, 10, 'a', 'new')],
                 schema='id INTEGER, amount INTEGER, category VARCHAR, status VARCHAR')
    configure(path, compare_fields=['status'], rules=[rule(max=2), rule('became_blank', max=0),
              rule('became_null', max=0), rule('percent_from_null', min=30, max=34),
              dict(name='metric', metric='amount', measure='delta', max=10)])
    out = tmp_path/'out'
    data = investigate(path, out)
    results = data['rule_checks']['results']
    assert [r['status'] for r in results] == ['failed', 'failed', 'passed', 'passed', 'passed']
    assert results[0]['numerator'] == 2 and results[0]['denominator'] == 3
    assert float(results[0]['observed']) == pytest.approx(200/3)
    assert results[1]['observed'] == 1 and results[1]['unit'] == 'rows'
    assert results[0]['evidence'] == 'analysis.sql: main.field_0'
    findings = json.loads((out/'findings.json').read_text())
    manifest = json.loads((out/'manifest.json').read_text())
    assert findings['rule_checks'] == manifest['rule_checks']
    html = (out/'report.html').read_text()
    assert 'Field: status' in html and '2 of 3 matched records (percent)' in html


@pytest.mark.parametrize('flag,expected', [(False, 0), (True, 3)])
def test_cli_writes_bundle_before_enforcement(tmp_path, flag, expected):
    path = setup(tmp_path, [(1, 10, 'a', 'open')], [(1, 10, 'a', 'closed')],
                 schema='id INTEGER, amount INTEGER, category VARCHAR, status VARCHAR')
    configure(path, compare_fields=['status'], rules=[rule(max=2)])
    out = tmp_path/'out'
    cmd = [sys.executable, '-m', 'delta_detective.cli', 'investigate', str(path), '--out', str(out)]
    if flag:
        cmd.append('--fail-on-rule-violation')
    result = subprocess.run(cmd, capture_output=True, text=True)
    assert result.returncode == expected, result.stderr
    assert '1 of 1 matched records' in result.stdout
    assert (out/'report.html').is_file()


def test_empty_population_count_zero_percent_undefined(tmp_path):
    path = setup(tmp_path, [], [], schema='id INTEGER, amount INTEGER, category VARCHAR, status VARCHAR')
    configure(path, compare_fields=['status'], rules=[rule(max=100), rule('changed_rows', min=0, max=0)])
    result = investigate(path, tmp_path/'out')['rule_checks']
    assert result['status'] == 'failed'
    assert result['results'][0]['status'] == 'undefined'
    assert result['results'][0]['observed'] is None
    assert result['results'][1]['observed'] == 0 and result['results'][1]['status'] == 'passed'


def test_later_dates_and_inclusive_bounds(tmp_path):
    path = setup(tmp_path, [(1, 10, 'a', date(2026, 1, 1)), (2, 10, 'a', None)],
                 [(1, 10, 'a', date(2026, 1, 2)), (2, 10, 'a', None)],
                 schema='id INTEGER, amount INTEGER, category VARCHAR, status DATE')
    configure(path, compare_fields=['status'], rules=[rule('increased_rows', min=1, max=1),
                                                     rule('percent_increased', min=50, max=50)])
    results = investigate(path, tmp_path/'out')['rule_checks']['results']
    assert all(r['status'] == 'passed' for r in results)
    assert results[1]['denominator'] == 2


@pytest.mark.parametrize('change', [dict(field='unknown'), dict(metric='amount'), dict(group_by=['category']),
    dict(where={'category': 'a'}), dict(measure='current_total'), dict(max=True), dict(max='NaN')])
def test_invalid_field_rules(tmp_path, change):
    path = setup(tmp_path, [], [], schema='id INTEGER, amount INTEGER, category VARCHAR, status VARCHAR')
    configure(path, compare_fields=['status'], rules=[dict(rule(max=1), **change)])
    with pytest.raises(InvestigationError):
        investigate(path, tmp_path/'out')
    assert not (tmp_path/'out').exists()


@pytest.mark.parametrize('typ,measure', [('BOOLEAN', 'increased_rows'), ('INTEGER', 'became_blank'),
                                        ('VARCHAR', 'percent_decreased'), ('DATE', 'percent_from_blank')])
def test_type_incompatible_measures_rejected(tmp_path, typ, measure):
    path = setup(tmp_path, [], [], schema=f'id INTEGER, amount INTEGER, category VARCHAR, status {typ}')
    configure(path, compare_fields=['status'], rules=[rule(measure, max=0)])
    with pytest.raises(InvestigationError, match='not supported for type'):
        investigate(path, tmp_path/'out')


def test_multiple_metrics_evaluate_field_rule_once_and_escape(tmp_path):
    path = setup(tmp_path, [(1, 10, 'a')], [(1, 10, 'b')])
    configure(path, compare_fields=['category'], metrics=[dict(name='rows', aggregate='count'),
              dict(name='amount', aggregate='sum', column='amount', null_policy='error')],
              rules=[dict(name='<script>', field='category', measure='changed_rows', max=0)])
    out = tmp_path/'out'
    result = investigate(path, out)['rule_checks']
    assert len(result['results']) == 1 and result['results'][0]['observed'] == 1
    html = (out/'report.html').read_text()
    assert '<script>' not in html and '&lt;script&gt;' in html
