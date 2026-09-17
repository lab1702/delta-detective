import json

import duckdb
import pytest

from delta_detective import investigate
from delta_detective.config import InvestigationError, load_config
from delta_detective.wizard import init_config
from test_features import configure
from test_investigation import setup


def test_transitions_null_empty_boolean_and_replay(tmp_path):
    ref = [(1, 1, None, False), (2, 1, '', None), (3, 1, 'NULL', True),
           (4, 1, 'same', True), (5, 1, 'removed_only', True)]
    cur = [(1, 1, '', True), (2, 1, 'NULL', False), (3, 1, None, True),
           (4, 1, 'same', True), (6, 1, 'added_only', True)]
    path = setup(tmp_path, ref, cur, schema='id INTEGER, amount INTEGER, category VARCHAR, active BOOLEAN',
                 dimensions=[], compare_fields=['category', 'active'], field_transitions=['category', 'active'])
    out = tmp_path / 'out'
    data = investigate(path, out)
    text, boolean = data['field_changes']['fields']
    rows = text['transitions']['rows']
    assert {(r['from_value'], r['to_value'], r['rows']) for r in rows} == {(None, '', 1), ('', 'NULL', 1), ('NULL', None, 1)}
    assert text['transitions']['changed_rows'] == text['changed_rows'] == 3
    assert [(r['from_value'], r['to_value']) for r in boolean['transitions']['rows']] == [(None, False), (False, True)]
    html = (out / 'report.html').read_text(encoding='utf-8')
    assert 'NULL (missing value)' in html and 'Value: ""' in html and 'Value: "NULL"' in html
    assert 'Value: false' in html and 'Value: true' in html
    assert 'added_only' not in html and 'removed_only' not in html
    manifest = json.loads((out / 'manifest.json').read_text())
    assert manifest['field_changes']['fields'][0]['transitions'] == text['transitions']
    assert not list(out.glob('*.csv'))
    with duckdb.connect() as con:
        con.execute((out / 'analysis.sql').read_text())
        assert con.execute('SELECT sum(rows) FROM field_0_transitions_display').fetchone() == (3,)


def test_bounded_results_and_deterministic_ties(tmp_path):
    ref = [(i, 1, f'old-{i:02}') for i in range(60)]
    cur = [(i, 1, f'new-{i:02}') for i in range(60)]
    path = setup(tmp_path, ref, cur, dimensions=[], compare_fields=['category'], field_transitions=['category'])
    first = investigate(path, tmp_path / 'one')['field_changes']['fields'][0]['transitions']
    second = investigate(path, tmp_path / 'two')['field_changes']['fields'][0]['transitions']
    assert first == second
    assert len(first['rows']) == 51
    assert first['distinct_transitions'] == 60 and first['omitted_transitions'] == 10
    assert first['rows'][0]['from_value'] == 'old-00'
    assert first['rows'][49]['from_value'] == 'old-49'
    assert first['rows'][-1] == dict(from_value=None, to_value=None, rows=10, rank=51, is_other=True)
    assert sum(r['rows'] for r in first['rows']) == 60


def test_counts_are_ranked_and_filters_apply_after_mapping(tmp_path):
    ref = [(1, 1, 'a'), (2, 1, 'a'), (3, 1, 'z'), (4, 0, 'excluded')]
    cur = [(1, 1, 'b'), (2, 1, 'b'), (3, 1, 'y'), (4, 0, 'excluded-new')]
    path = setup(tmp_path, ref, cur, dimensions=[], compare_fields=['status'], field_transitions=['status'],
                 column_mapping={s: {'category': 'status'} for s in ('reference', 'current')},
                 filters=[dict(column='amount', operator='gt', value=0)])
    data = investigate(path, tmp_path / 'out')
    rows = data['field_changes']['fields'][0]['transitions']['rows']
    assert [(r['from_value'], r['rows']) for r in rows] == [('a', 2), ('z', 1)]


def test_disabled_does_not_expose_field_values(tmp_path):
    path = setup(tmp_path, [(1, 1, 'private_old_sentinel')], [(1, 1, 'private_new_sentinel')],
                 dimensions=[], compare_fields=['category'])
    out = tmp_path / 'out'
    data = investigate(path, out)
    assert 'transitions' not in data['field_changes']['fields'][0]
    for file in out.iterdir():
        assert 'private_old_sentinel' not in file.read_text(encoding='utf-8')
        assert 'private_new_sentinel' not in file.read_text(encoding='utf-8')


@pytest.mark.parametrize('rows', [[], [(1, 1, 'same')]])
def test_empty_and_unchanged(tmp_path, rows):
    path = setup(tmp_path, rows, rows, compare_fields=['category'], field_transitions=['category'])
    out = tmp_path / 'out'
    t = investigate(path, out)['field_changes']['fields'][0]['transitions']
    assert t['rows'] == [] and t['changed_rows'] == t['distinct_transitions'] == 0
    assert 'No changed matched records' in (out / 'report.html').read_text()


@pytest.mark.parametrize('spec', [None, 'category', {}, ['category', 'category'], ['missing'], [1]])
def test_invalid_config(tmp_path, spec):
    path = setup(tmp_path, [], [], compare_fields=['category'], field_transitions=spec)
    with pytest.raises(InvestigationError):
        load_config(path)


def test_numeric_field_rejected_and_previous_bundle_preserved(tmp_path):
    path = setup(tmp_path, [(1, 1, 'a')], [(1, 2, 'a')], compare_fields=['amount'])
    out = tmp_path / 'out'
    investigate(path, out)
    before = {p.name: p.read_bytes() for p in out.iterdir()}
    configure(path, field_transitions=['amount'])
    with pytest.raises(InvestigationError, match='text or boolean'):
        investigate(path, out, overwrite=True)
    assert {p.name: p.read_bytes() for p in out.iterdir()} == before


def test_html_values_are_escaped(tmp_path):
    path = setup(tmp_path, [(1, 1, '<script>alert(1)</script>')], [(1, 1, 'Other (remainder)')],
                 dimensions=[], compare_fields=['category'], field_transitions=['category'])
    out = tmp_path / 'out'
    row = investigate(path, out)['field_changes']['fields'][0]['transitions']['rows'][0]
    assert row['is_other'] is False
    assert '<script>' not in (out / 'report.html').read_text()
    assert row['from_value'] == '<script>alert(1)</script>'


def test_wizard_transition_opt_in(tmp_path):
    setup(tmp_path, [(1, 1, 'a')], [(1, 1, 'b')])
    answers = iter(['', '1', '1', '', '', '', 'yes', '', '2', '1', '', ''])
    messages = []
    path = init_config(tmp_path / 'reference.parquet', tmp_path / 'current.parquet', tmp_path / 'new.yaml',
                       ask=lambda p: next(answers), tell=messages.append)
    assert list(answers) == []
    assert load_config(path)['field_transitions'] == ['category']
    assert any('expose source and destination values' in m for m in messages)
    assert investigate(path, tmp_path / 'out')['field_changes']['fields'][0]['transitions']['changed_rows'] == 1
