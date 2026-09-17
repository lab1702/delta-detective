import csv
import json

import duckdb
import pytest

from delta_detective import investigate
from delta_detective.config import InvestigationError
from test_features import configure
from test_investigation import setup


def rows(path):
    with path.open(newline='', encoding='utf-8') as stream:
        return list(csv.DictReader(stream))


def test_field_rule_exact_population_limit_and_replay(tmp_path):
    path = setup(tmp_path, [(1, 10, 'a', 'secret'), (2, 10, 'a', 'old'), (3, 10, 'a', None), (4, 10, 'a', 'x')],
                 [(1, 10, 'a', None), (2, 10, 'a', None), (3, 10, 'a', None), (4, 10, 'a', 'y')],
                 schema='id INTEGER, amount INTEGER, category VARCHAR, status VARCHAR')
    configure(path, compare_fields=['status'], rules=[dict(name='nulls', field='status', measure='became_null', max=0, export={'limit': 1})])
    out = tmp_path/'out'
    data = investigate(path, out)
    exported = rows(out/'rule_0_rows.csv')
    assert len(exported) == 1 and exported[0]['k0'] == '1' and exported[0]['rf0'] == 'secret'
    info = data['rule_checks']['results'][0]['evidence_export']
    assert (info['rows'], info['total_rows'], info['truncated']) == (1, 2, True)
    assert data['evidence_exports'] == [info]
    manifest = json.loads((out/'manifest.json').read_text())
    assert manifest['raw_evidence']['included'] is True
    assert 'secret' not in (out/'report.html').read_text()
    assert 'href="rule_0_rows.csv"' in (out/'report.html').read_text()
    with duckdb.connect() as con:
        con.execute((out/'analysis.sql').read_text())
        assert con.execute('SELECT k0 FROM main.rule_0_evidence_all ORDER BY k0').fetchall() == [(1,), (2,)]
        assert con.execute('SELECT k0 FROM main.rule_0_evidence').fetchall() == [(1,)]


def test_segment_removals_include_moves_but_not_passed_segments(tmp_path):
    path = setup(tmp_path, [(1, 10, 'West'), (2, 10, 'West'), (3, 10, 'West'), (4, 10, 'East')],
                 [(1, 10, 'South'), (2, 10, 'West'), (4, 10, 'East')])
    configure(path, rules=[dict(name='retention', metric='amount', measure='removed_percent', group_by=['category'], max=5, export={})])
    out = tmp_path/'out'
    data = investigate(path, out)
    assert [r['k0'] for r in rows(out/'rule_0_rows.csv')] == ['1', '3']
    info = data['rule_checks']['results'][0]['evidence_export']
    assert info['limit'] == 100 and info['truncated'] is False
    with duckdb.connect() as con:
        con.execute((out/'analysis.sql').read_text())
        assert con.execute('SELECT count(*) FROM main.rule_0_evidence').fetchone() == (2,)


def test_every_failed_segment_is_exported_beyond_display_cap(tmp_path):
    path = setup(tmp_path, [(i, 10, f'c{i:03}') for i in range(120)], [(i, 8, f'c{i:03}') for i in range(120)])
    configure(path, rules=[dict(name='change', metric='amount', measure='abs_percent_change', group_by=['category'], max=10, export={'limit': 200})])
    out = tmp_path/'out'
    data = investigate(path, out)
    result = data['rule_checks']['results'][0]
    assert len(result['segments']) == 50 and result['omitted_segments'] == 70
    assert result['evidence_export']['total_rows'] == 120
    assert len(rows(out/'rule_0_rows.csv')) == 120


def test_combined_null_scope_multiple_metrics_and_deduplication(tmp_path):
    path = setup(tmp_path, [(1, 10, None, 'web'), (2, 10, 'B', 'web')],
                 [(1, 10, 'B', 'web'), (2, 10, None, 'web')],
                 schema='id INTEGER, amount INTEGER, category VARCHAR, source VARCHAR')
    configure(path, metrics=[dict(name='amount', aggregate='sum', column='amount', null_policy='error'), dict(name='rows', aggregate='count')],
              dimension_groups=[['category', 'source']], rules=[dict(name='removed', metric='rows', measure='removed_rows', max=0,
                  group_by=['category', 'source'], where={'category': None, 'source': 'web'}, export={})])
    out = tmp_path/'out'
    data = investigate(path, out)
    assert [r['k0'] for r in rows(out/'rule_0_rows.csv')] == ['1']
    assert data['evidence_exports'][0]['evidence'] == 'metric_1.rule_0_evidence'


def test_passed_rule_skips_export_and_missing_population_header_only(tmp_path):
    path = setup(tmp_path, [], [])
    configure(path, rules=[dict(name='passes', metric='amount', measure='current_rows', max=0, export={}),
                           dict(name='missing', metric='amount', measure='current_rows', min=1, export={}),
                           dict(name='undefined', metric='amount', measure='percent_change', max=1, export={})])
    out = tmp_path/'out'
    data = investigate(path, out)
    assert not (out/'rule_0_rows.csv').exists()
    assert data['rule_checks']['results'][0]['evidence_export']['status'] == 'skipped'
    assert rows(out/'rule_1_rows.csv') == rows(out/'rule_2_rows.csv') == []
    assert all(not e['truncated'] for e in data['evidence_exports'])


@pytest.mark.parametrize('export', [True, None, {'limit': 0}, {'limit': -1}, {'limit': True}, {'limit': '2'}, {'path': '../bad.csv'}])
def test_invalid_export_config(tmp_path, export):
    path = setup(tmp_path, [], [])
    configure(path, rules=[dict(name='bad', metric='amount', measure='current_rows', max=0, export=export)])
    with pytest.raises(InvestigationError):
        investigate(path, tmp_path/'out')


def test_schema_failure_and_opt_out_produce_no_rule_csv(tmp_path):
    path = setup(tmp_path, [(1, 10, 'a')], [(1, 10, 'a')])
    configure(path, rules=[dict(name='bad', metric='amount', measure='current_rows', max=0)])
    out = tmp_path/'out'
    assert investigate(path, out)['evidence_exports'] == []
    assert not list(out.glob('*.csv'))
    configure(path, schema={'columns': {'id': 'VARCHAR'}}, rules=[dict(name='bad', metric='amount', measure='current_rows', max=0, export={})])
    investigate(path, tmp_path/'schema')
    assert not list((tmp_path/'schema').glob('*.csv'))
