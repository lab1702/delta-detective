import json
from html.parser import HTMLParser

from delta_detective import investigate
from delta_detective.demo import demo
from test_features import configure
from test_investigation import setup


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.links = []

    def handle_starttag(self, tag, attrs):
        for key, value in attrs:
            if key == 'id':
                self.ids.append(value)
            if key == 'href':
                self.links.append(value)


def check_links(out):
    parser = Links()
    html = (out/'report.html').read_text(encoding='utf-8')
    parser.feed(html)
    assert len(parser.ids) == len(set(parser.ids))
    for link in parser.links:
        if link.startswith('#'):
            assert link[1:] in parser.ids, link
        else:
            assert link.endswith('.csv') and (out/link).is_file()
    assert html.index('id="investigation-summary"') < html.index('Field-level changes:')


def test_demo_summary_uses_verified_totals(tmp_path):
    path = demo(tmp_path/'demo')
    out = tmp_path/'out'
    data = investigate(path, out)
    summary = data['investigation_summary']
    assert summary['status'] == 'completed'
    assert 'no threshold rules' in summary['headline']
    assert summary['population'] == dict(added_rows=1, removed_rows=1, matched_rows=3)
    assert summary['metrics'][0]['delta'] == -18000
    assert summary['metrics'][0]['percent_change'] == -18
    assert all(s['contribution'] != 0 for s in summary['metrics'][0]['segments'])
    findings = json.loads((out/'findings.json').read_text())
    manifest = json.loads((out/'manifest.json').read_text())
    assert findings['investigation_summary'] == manifest['investigation_summary']
    check_links(out)


def test_failed_field_segment_rules_exports_and_links(tmp_path):
    path = setup(tmp_path, [(1, 100, 'West', 'PRIVATE_NOTE_82391')], [(1, 80, 'West', None)],
                 schema='id INTEGER, amount INTEGER, category VARCHAR, note VARCHAR')
    configure(path, compare_fields=['note'], rules=[
        dict(name='passes', metric='amount', measure='current_rows', max=1),
        dict(name='West change', metric='amount', group_by=['category'], measure='abs_percent_change', max=5),
        dict(name='Missing notes', field='note', measure='became_null', max=0, export={})])
    out = tmp_path/'out'
    data = investigate(path, out)
    summary = data['investigation_summary']
    assert summary['status'] == 'review'
    assert [r['href'] for r in summary['rules']['attention']] == ['#rule-1', '#rule-2']
    assert summary['fields'][0]['became_null'] == 1
    html = (out/'report.html').read_text()
    assert 'PRIVATE_NOTE_82391' not in html
    assert 'Bounds: ' in html and 'max 5' in html
    check_links(out)


def test_schema_blocked_summary_never_claims_comparison(tmp_path):
    path = setup(tmp_path, [], [])
    configure(path, schema={'columns': {'<script>': None}})
    out = tmp_path/'out'
    summary = investigate(path, out)['investigation_summary']
    assert summary['status'] == 'blocked' and summary['metrics'] == []
    assert summary['schema_violation_count'] == 2
    assert summary['population'] is None
    html = (out/'report.html').read_text()
    assert '<script>' not in html and '&lt;script&gt;' in html
    assert 'Arithmetic reconciliation passed for every configured metric.' not in html
    check_links(out)


def test_caps_other_remainder_and_undefined_rules(tmp_path):
    path = setup(tmp_path, [], [(i, 1, str(i)) for i in range(100)])
    configure(path, rules=[dict(name=f'r{i}', metric='amount', measure='percent_change', max=1) for i in range(12)])
    out = tmp_path/'out'
    summary = investigate(path, out)['investigation_summary']
    assert summary['status'] == 'review'
    assert summary['rules']['undefined'] == 12
    assert summary['rules']['omitted'] == 2 and len(summary['rules']['attention']) == 10
    assert summary['metrics'][0]['percent_change'] is None
    assert len(summary['metrics'][0]['segments']) == 3
    assert all(s['category'] is not None and s['contribution'] == 1 for s in summary['metrics'][0]['segments'])
    check_links(out)


def test_multiple_metrics_unchanged_and_passed_rules(tmp_path):
    path = setup(tmp_path, [(1, 100, 'a')], [(1, 100, 'a')])
    configure(path, metrics=[dict(name='money', aggregate='sum', column='amount', null_policy='error'),
                             dict(name='rows', aggregate='count')],
              rules=[dict(name='same', metric='money', measure='abs_delta', max=0)])
    out = tmp_path/'out'
    summary = investigate(path, out)['investigation_summary']
    assert summary['status'] == 'passed'
    assert [m['name'] for m in summary['metrics']] == ['money', 'rows']
    assert all(m['delta'] == 0 and not m['segments'] for m in summary['metrics'])
    check_links(out)
