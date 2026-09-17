"""Deterministic report highlights drawn only from verified run results."""
from decimal import Decimal


def build_summary(data):
    blocked = data['execution_status'] == 'schema_contract_failed'
    rules = data['rule_checks']['results']
    attention = [dict(r, href=f'#rule-{i}') for i, r in enumerate(rules) if r['status'] != 'passed']
    violations = [r for r in data['schema_checks']['results'] if r['status'] == 'failed']
    if blocked:
        status, headline = 'blocked', 'Comparison blocked by schema contract'
    elif attention:
        status, headline = 'review', 'Comparison completed; threshold rules need review'
    elif rules:
        status, headline = 'passed', 'Comparison completed; all configured rules passed'
    else:
        status, headline = 'completed', 'Comparison completed; no threshold rules configured'
    metrics = []
    for i, item in enumerate(data['metrics']):
        s = item['summary']
        segments = []
        for j, dimension in enumerate(item['dimensions']):
            for row in dimension['rows']:
                if not row['is_other'] and row['contribution'] != 0:
                    segments.append(dict(dimension=dimension['name'], columns=dimension['columns'],
                                         category=row['category'], contribution=row['contribution'],
                                         href=f'#breakdown-{i}-{j}'))
        segments.sort(key=lambda r: Decimal(str(r['contribution'])).copy_abs(), reverse=True)
        metrics.append(dict(name=item['metric']['name'], reference=s['reference_total'], current=s['current_total'],
                            delta=s['delta'], percent_change=s['percent_change'], exact=s['exact'],
                            href=f'#metric-{i}', segments=segments[:3],
                            additional_segment_highlights=max(0, len(segments)-3)))
    fields = [dict(f, href=f'#field-{i}') for i, f in enumerate(data['field_changes']['fields']) if f['changed_rows']]
    fields.sort(key=lambda f: f['changed_rows'], reverse=True)
    population = None if blocked else {k: data['summary'][k] for k in ('added_rows', 'removed_rows', 'matched_rows')}
    return dict(status=status, headline=headline, population=population,
                rules=dict(total=len(rules), passed=sum(r['status'] == 'passed' for r in rules),
                           failed=sum(r['status'] == 'failed' for r in rules),
                           undefined=sum(r['status'] == 'undefined' for r in rules),
                           attention=attention[:10], omitted=max(0, len(attention)-10)),
                schema_violations=violations[:10], schema_violation_count=len(violations),
                metrics=metrics, fields=fields[:5], omitted_fields=max(0, len(fields)-5),
                evidence_files=len(data['evidence_exports']),
                limitations='Observed changes and policy results do not establish causes. Metrics may use different units; dimension views overlap and must not be added together.')
