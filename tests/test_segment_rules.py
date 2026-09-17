import json
import subprocess
import sys

import duckdb
import pytest

from delta_detective import investigate
from delta_detective.config import InvestigationError
from test_features import configure
from test_investigation import setup


def rule(measure='abs_percent_change', **extra):
    return dict(name='segments', metric='amount', measure=measure, group_by=['category'], max=10, **extra)


def test_offsetting_changes_and_cli(tmp_path):
    path = setup(tmp_path, [(1, 100, 'West'), (2, 100, 'East')], [(1, 80, 'West'), (2, 120, 'East')])
    configure(path, rules=[rule()])
    out = tmp_path/'out'
    result = subprocess.run([sys.executable, '-m', 'delta_detective.cli', 'investigate', str(path), '--out', str(out), '--fail-on-rule-violation'], capture_output=True, text=True)
    assert result.returncode == 3, result.stderr
    data = json.loads((out/'findings.json').read_text())
    assert data['summary']['delta'] == '0.00'
    checked = data['rule_checks']['results'][0]
    assert checked['segment_counts'] == dict(passed=0, failed=2, undefined=0)
    assert {s['segment']['category'] for s in checked['segments']} == {'West', 'East'}
    assert all(s['observed'] == '20.0' or float(s['observed']) == 20 for s in checked['segments'])
    assert 'Segment rule: segments' in (out/'report.html').read_text()
    with duckdb.connect() as con:
        con.execute((out/'analysis.sql').read_text())
        assert con.execute('SELECT count(*) FROM main.segment_rule_0').fetchone() == (2,)


def test_membership_removals_include_moves(tmp_path):
    path = setup(tmp_path, [(1, 10, 'A'), (2, 20, 'A'), (3, 5, 'A')], [(1, 10, 'B'), (2, 20, 'A')])
    configure(path, rules=[rule('removed_percent', where={'category': 'A'})])
    result = investigate(path, tmp_path/'out')['rule_checks']['results'][0]
    assert result['status'] == 'failed' and result['total_segments'] == 1
    assert float(result['segments'][0]['observed']) == pytest.approx(200/3)


def test_all_segments_checked_beyond_display_limit(tmp_path):
    before = [(i, 100, f'c{i:03}') for i in range(120)]
    after = [(i, 100 if i < 119 else 85, f'c{i:03}') for i in range(120)]
    path = setup(tmp_path, before, after)
    configure(path, rules=[rule()])
    result = investigate(path, tmp_path/'out')['rule_checks']['results'][0]
    assert result['total_segments'] == 120 and result['omitted_segments'] == 70
    assert result['segment_counts'] == dict(passed=119, failed=1, undefined=0)
    assert result['segments'][0]['segment'] == {'category': 'c119'}
    assert result['segments'][0]['status'] == 'failed'


def test_combined_null_selection_and_multiple_metrics(tmp_path):
    path = setup(tmp_path, [(1, 10, None, 'web'), (2, 10, '(NULL)', 'web')],
                 [(1, 5, None, 'web'), (2, 20, '(NULL)', 'web')],
                 schema='id INTEGER, amount DECIMAL(18,2), category VARCHAR, source VARCHAR')
    configure(path, metrics=[dict(name='rows', aggregate='count'), dict(name='amount', aggregate='sum', column='amount', null_policy='error')],
              dimension_groups=[['category', 'source']], rules=[dict(rule(), group_by=['category', 'source'], where={'category': None, 'source': 'web'})])
    out = tmp_path/'out'
    result = investigate(path, out)['rule_checks']['results'][0]
    assert result['total_segments'] == 1
    assert result['segments'][0]['observed'] == 50
    assert result['evidence'] == 'analysis.sql: metric_1.segment_rule_0'


@pytest.mark.parametrize('where', [None, {'category': 'missing'}])
def test_empty_scope_is_undefined(tmp_path, where):
    path = setup(tmp_path, [], [])
    r = rule()
    if where is not None:
        r['where'] = where
    configure(path, rules=[r])
    result = investigate(path, tmp_path/'out')['rule_checks']
    assert result['status'] == 'failed'
    assert result['results'][0]['status'] == 'undefined'


def test_new_disappeared_zero_and_escaped_categories(tmp_path):
    path = setup(tmp_path, [(1, 10, 'gone'), (2, 0, 'zero')], [(2, 0, 'zero'), (3, 10, '<script>')])
    configure(path, rules=[rule()])
    out = tmp_path/'out'
    result = investigate(path, out)['rule_checks']['results'][0]
    by_category = {s['segment']['category']: s for s in result['segments']}
    assert by_category['gone']['observed'] == 100
    assert by_category['zero']['status'] == by_category['<script>']['status'] == 'undefined'
    assert '<script>' not in (out/'report.html').read_text()


@pytest.mark.parametrize('change', [dict(group_by=[]), dict(group_by=['unknown']), dict(group_by=['id']),
    dict(where={'wrong': 'A'}), dict(where=[]), dict(where={'category': []}), dict(where={'category': True})])
def test_invalid_scope(tmp_path, change):
    path = setup(tmp_path, [(1, 10, 'A')], [(1, 10, 'A')])
    configure(path, rules=[dict(rule(), **change)])
    with pytest.raises(InvestigationError):
        investigate(path, tmp_path/'out')
    assert not (tmp_path/'out').exists()


def test_numeric_selector_is_exact_not_rounded(tmp_path):
    path = setup(tmp_path, [(1, 10, '1.25')], [(1, 10, '1.25')], schema='id INTEGER, amount DECIMAL(18,2), category DECIMAL(10,2)')
    configure(path, rules=[rule(where={'category': '1.251'})])
    assert investigate(path, tmp_path/'out')['rule_checks']['results'][0]['status'] == 'undefined'
