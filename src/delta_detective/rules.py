"""Evaluate user policy independently of arithmetic/data validation."""
from decimal import Decimal, localcontext

from .config import threshold_number, InvestigationError, selected_dimensions, FIELD_PERCENTAGES


def evaluate_rules(rules, results, con=None, cfg=None, execute=None, field_changes=None):
    metrics = {item["metric"]["name"]: item for item in results}
    evaluated = []
    for index, rule in enumerate(rules):
        if 'field' in rule:
            field = next(f for f in field_changes['fields'] if f['name'] == rule['field'])
            evaluated.append(evaluate_field_rule(rule, field))
            continue
        item = metrics[rule["metric"]]
        if "group_by" in rule:
            evaluated.append(evaluate_segments(rule, item, index, con, cfg, execute))
            continue
        summary = item["summary"]
        measure = rule["measure"]
        reason = None
        # Match the precision used by reconciliation and percentage reporting.
        with localcontext() as ctx:
            ctx.prec = 100
            if measure == "removed_percent":
                value = (Decimal(summary["removed_rows"]) * 100 / summary["reference_rows"]
                         if summary["reference_rows"] else None)
                if value is None:
                    reason = "Undefined: reference row count is zero."
            elif measure in ("abs_delta", "abs_percent_change"):
                value = summary[measure[4:]]
                value = abs(value) if value is not None else None
            else:
                value = summary[measure]
            if value is None and reason is None:
                reason = "Undefined: reference metric total is zero."
            bounds = {k: threshold_number(rule[k]) for k in ("min", "max") if k in rule}
            observed = Decimal(str(value)) if value is not None else None
            status = "undefined" if observed is None else "passed"
            if observed is not None and (("min" in bounds and observed < bounds["min"])
                                         or ("max" in bounds and observed > bounds["max"])):
                status = "failed"
                reason = "Observed value is outside the inclusive bounds."
        evaluated.append({"name": rule["name"], "metric": rule["metric"], "measure": measure,
                          "observed": value, **bounds, "status": status, "reason": reason,
                          "exact": summary["exact"] if measure not in ("removed_percent", "current_rows", "added_rows", "removed_rows") else True,
                          "evidence": f"analysis.sql: {item['sql_schema']}.reconciliation"})
    return {"status": ("not_configured" if not evaluated else
                       "failed" if any(r["status"] != "passed" for r in evaluated) else "passed"),
            "results": evaluated}


def evaluate_field_rule(rule, field):
    measure = rule['measure']
    numerator = field[FIELD_PERCENTAGES.get(measure, measure)]
    denominator = field['matched_rows']
    percentage = measure in FIELD_PERCENTAGES
    with localcontext() as ctx:
        ctx.prec = 100
        value = (Decimal(numerator) * 100 / denominator if denominator else None) if percentage else numerator
    bounds = {k: threshold_number(rule[k]) for k in ('min', 'max') if k in rule}
    status, reason = 'passed', None
    if value is None:
        status, reason = 'undefined', 'Undefined: no matched records.'
    elif any((value < bound if k == 'min' else value > bound) for k, bound in bounds.items()):
        status, reason = 'failed', 'Observed value is outside the inclusive bounds.'
    return dict(name=rule['name'], field=rule['field'], measure=measure, observed=value,
                **bounds, numerator=numerator, denominator=denominator,
                denominator_basis='all matched records', unit='percent' if percentage else 'rows',
                status=status, reason=reason, exact=True,
                evidence=f"analysis.sql: {field['evidence']}", denominator_evidence='analysis.sql: main.field_overview')


def evaluate_segments(rule, item, index, con, cfg, execute):
    group = rule['group_by']
    indices = [selected_dimensions(cfg).index(c) for c in group]
    ns = item['sql_schema']
    table = f'{ns}.segment_rule_{index}'
    schema = dict((r[0], r[1]) for r in con.execute('DESCRIBE main.reference').fetchall())
    selector = {}
    for column, value in rule.get('where', {}).items():
        typ = schema[column]
        if value is None:
            selector[column] = None
        elif typ == 'VARCHAR' and isinstance(value, str):
            selector[column] = value
        elif typ == 'BOOLEAN' and type(value) is bool:
            selector[column] = value
        elif typ not in ('VARCHAR', 'BOOLEAN') and type(value) is not bool:
            selector[column] = threshold_number(value)
        else:
            raise InvestigationError(f'Segment selector type does not match column {column!r}')
    changed = ' OR '.join(f'rd{i} IS DISTINCT FROM cd{i}' for i in indices)
    categories = [f'g{i}' for i in range(len(group))]
    reference = ', '.join(f'rd{j} AS g{i}' for i, j in enumerate(indices))
    current = ', '.join(f'cd{j} AS g{i}' for i, j in enumerate(indices))
    execute(f"""CREATE TABLE {table} AS WITH memberships AS (
      SELECT {reference}, rv AS reference_total, 0 AS current_total,
        1 AS reference_rows, 0 AS current_rows, 0 AS added_rows,
        CASE WHEN cp IS NULL OR ({changed}) THEN 1 ELSE 0 END AS removed_rows
      FROM {ns}.joined WHERE rp
      UNION ALL
      SELECT {current}, 0, cv, 0, 1,
        CASE WHEN rp IS NULL OR ({changed}) THEN 1 ELSE 0 END, 0
      FROM {ns}.joined WHERE cp)
      SELECT {', '.join(categories)}, sum(reference_total) AS reference_total,
        sum(current_total) AS current_total, sum(reference_rows) AS reference_rows,
        sum(current_rows) AS current_rows, sum(added_rows) AS added_rows,
        sum(removed_rows) AS removed_rows
      FROM memberships GROUP BY {', '.join(categories)}""")
    cursor = con.execute(f"SELECT * FROM {table} ORDER BY " + ', '.join(c + ' NULLS FIRST' for c in categories))
    fields = [c[0] for c in cursor.description]
    counts = dict(passed=0, failed=0, undefined=0)
    samples = {status: [] for status in counts}
    plain_rule = {k: v for k, v in rule.items() if k not in ('group_by', 'where')}
    # Evaluate all aggregate segments in bounded batches; never materialize source
    # records or an unbounded list of segment results in Python.
    while batch := cursor.fetchmany(50):
        for values in batch:
            record = dict(zip(fields, values))
            segment = {c: record[f'g{i}'] for i, c in enumerate(group)}
            if selector and any(segment[c] != value for c, value in selector.items()):
                continue
            with localcontext() as ctx:
                ctx.prec = 100
                before, after = record['reference_total'], record['current_total']
                if not all(Decimal(str(v)).is_finite() for v in (before, after)):
                    raise InvestigationError('Nonfinite segment total; use exact decimal inputs')
                record['delta'] = after - before
                record['percent_change'] = Decimal(str(record['delta'])) / Decimal(str(before)) * 100 if before else None
            record['exact'] = item['summary']['exact']
            detail = evaluate_rules([plain_rule], [dict(item, summary=record)])['results'][0]
            detail.update(segment=segment, evidence=f'analysis.sql: {table}')
            counts[detail['status']] += 1
            if len(samples[detail['status']]) < 50:
                samples[detail['status']].append(detail)
    total = sum(counts.values())
    details = (samples['failed'] + samples['undefined'] + samples['passed'])[:50]
    reason = 'No matching segments in either snapshot.' if not total else None
    return dict(name=rule['name'], metric=rule['metric'], measure=rule['measure'],
                **{k: threshold_number(rule[k]) for k in ('min', 'max') if k in rule},
                group_by=group, where=rule.get('where'), observed=None,
                status='undefined' if not total else 'failed' if counts['failed'] or counts['undefined'] else 'passed',
                reason=reason, exact=item['summary']['exact'], evidence=f'analysis.sql: {table}',
                segment_counts=counts, total_segments=total, segments=details,
                omitted_segments=total-len(details))
