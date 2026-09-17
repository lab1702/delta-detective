"""Stream opt-in supporting rows for failed rules without retaining raw rows."""
from .config import FIELD_PERCENTAGES, selected_dimensions
from .loading import literal
from .tolerances import conditions_for


def export_rule_evidence(con, cfg, results, checks, stage, execute):
    metrics = {r['metric']['name']: r for r in results}
    schema = {r[0]: r[1] for r in con.execute('DESCRIBE main.reference').fetchall()}
    exports = []
    for i, (rule, result) in enumerate(zip(cfg['rules'], checks['results'])):
        if 'export' not in rule:
            continue
        if result['status'] == 'passed':
            result['evidence_export'] = {'status': 'skipped', 'reason': 'Rule passed.'}
            continue
        measure = rule['measure']
        if 'field' in rule:
            ns = 'main'
            j = cfg['compare_fields'].index(rule['field'])
            predicate = 'j.rp AND j.cp AND (' + conditions_for(cfg, rule['field'], f'j.rf{j}', f'j.cf{j}', schema[rule['field']])[FIELD_PERCENTAGES.get(measure, measure)] + ')'
            selection = 'Matched records counted by the field measure (the percentage numerator when applicable).'
        else:
            ns = metrics[rule['metric']]['sql_schema']
            predicate = {'removed_rows': 'j.cp IS NULL', 'removed_percent': 'j.cp IS NULL',
                         'added_rows': 'j.rp IS NULL', 'current_rows': 'j.cp', 'current_total': 'j.cp'}.get(measure, 'true')
            selection = 'Rows contributing to the measured population; before/after fields provide context, not individual violations.'
            if 'group_by' in rule:
                indices = [selected_dimensions(cfg).index(c) for c in rule['group_by']]
                old = 'j.rp AND ' + ' AND '.join(f'j.rd{k} IS NOT DISTINCT FROM s.g{n}' for n, k in enumerate(indices))
                new = 'j.cp AND ' + ' AND '.join(f'j.cd{k} IS NOT DISTINCT FROM s.g{n}' for n, k in enumerate(indices))
                # Side presence is nullable; coalesce before negating membership.
                old, new = f'coalesce(({old}), false)', f'coalesce(({new}), false)'
                membership = (f'{old} AND NOT {new}' if measure in ('removed_rows', 'removed_percent') else
                              f'{new} AND NOT {old}' if measure == 'added_rows' else
                              new if measure in ('current_rows', 'current_total') else f'({old} OR {new})')
                predicate = (f'EXISTS (SELECT 1 FROM {ns}.segment_rule_{i} s '
                             f'JOIN {ns}.rule_{i}_failed_segments f USING (segment_id) WHERE {membership})')
                selection += ' Only failed or undefined selected segments are included; keys are deduplicated across segments.'
        eligible = f'{ns}.rule_{i}_evidence_all'
        view = f'{ns}.rule_{i}_evidence'
        execute(f'CREATE VIEW {eligible} AS SELECT j.* FROM {ns}.joined j WHERE {predicate}')
        total = con.execute(f'SELECT count(*) FROM {eligible}').fetchone()[0]
        limit = rule['export']['limit']
        order = ', '.join(f'k{k}' for k in range(len(cfg['key'])))
        execute(f'CREATE VIEW {view} AS SELECT * FROM {eligible} ORDER BY {order} LIMIT {limit}')
        filename = f'rule_{i}_rows.csv'
        con.execute(f'COPY (SELECT * FROM {view} ORDER BY {order}) TO {literal(stage / filename)} (HEADER, FORMAT CSV)')
        record = dict(file=filename, kind='rule', rule=rule['name'], rule_index=i,
                      metric=rule.get('metric'), field=rule.get('field'), limit=limit,
                      rows=min(total, limit), total_rows=total, truncated=total > limit,
                      evidence=view, selection=selection, status='exported')
        result['evidence_export'] = record
        exports.append(record)
    return exports
