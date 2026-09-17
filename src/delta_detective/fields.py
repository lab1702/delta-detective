"""Aggregate comparisons of selected non-key fields on matched records."""
from decimal import Decimal, localcontext

from .config import InvestigationError
from .tolerances import conditions_for, any_field_changed


def compare_fields(con, cfg, profiles, execute):
    fields = cfg.get('compare_fields', [])
    if not fields:
        return {'status': 'not_configured', 'fields': []}
    changed = any_field_changed(cfg, profiles['reference']['schema'])
    execute(f"""CREATE TABLE main.field_overview AS SELECT count(*) AS matched_rows,
      count(*) FILTER (WHERE {changed}) AS changed_rows
      FROM main.joined WHERE rp AND cp""")
    matched, any_changed = con.execute('SELECT * FROM main.field_overview').fetchone()
    results = []
    for i, name in enumerate(fields):
        typ = profiles['reference']['schema'][name]
        before, after = f'rf{i}', f'cf{i}'
        all_conditions = conditions_for(cfg, name, before, after, typ)
        labels = ['changed_rows', 'unchanged_rows', 'exact_match_rows', 'within_tolerance_rows',
                  'became_null', 'from_null', 'both_null', 'value_changed_rows']
        if typ == 'VARCHAR':
            labels += ['became_blank', 'from_blank']
        elif typ != 'BOOLEAN':
            labels += ['increased_rows', 'decreased_rows']
        conditions = {label: all_conditions[label] for label in labels}
        table = f'main.field_{i}'
        execute(f"CREATE TABLE {table} AS SELECT " + ', '.join(
            f'count(*) FILTER (WHERE {condition}) AS {label}' for label, condition in conditions.items())
            + ' FROM main.joined WHERE rp AND cp')
        cur = con.execute(f'SELECT * FROM {table}')
        counts = dict(zip([c[0] for c in cur.description], cur.fetchone()))
        if (counts['changed_rows'] + counts['unchanged_rows'] != matched
                or counts['unchanged_rows'] != counts['exact_match_rows'] + counts['within_tolerance_rows']
                or counts['changed_rows'] != counts['became_null'] + counts['from_null'] + counts['value_changed_rows']):
            raise InvestigationError('Field comparison row-count identity failed')
        with localcontext() as ctx:
            ctx.prec = 100
            percent = Decimal(counts['changed_rows']) * 100 / matched if matched else None
        results.append(dict(name=name, type=typ, matched_rows=matched, **counts,
                            percent_changed=percent, evidence=table,
                            absolute_tolerance=cfg.get('field_tolerances', {}).get(name, {}).get('absolute')))
        if name in cfg.get('field_transitions', []):
            results[-1]['transitions'] = field_transitions(con, execute, i, counts['changed_rows'])
    return dict(status='passed', matched_rows=matched, changed_rows=any_changed,
                unchanged_rows=matched-any_changed, fields=results, evidence='main.field_overview')


def field_transitions(con, execute, index, expected_rows):
    """Return at most 50 transitions plus Other; keep all grouping inside DuckDB."""
    table = f'main.field_{index}_transitions'
    execute(f'''CREATE TABLE {table} AS SELECT rf{index} AS from_value, cf{index} AS to_value,
      count(*) AS rows FROM main.joined
      WHERE rp AND cp AND rf{index} IS DISTINCT FROM cf{index} GROUP BY 1, 2''')
    execute(f'''CREATE TABLE {table}_ranked AS SELECT *, row_number() OVER (
      ORDER BY rows DESC, from_value NULLS FIRST, to_value NULLS FIRST) AS rank FROM {table}''')
    execute(f'''CREATE TABLE {table}_display AS
      SELECT *, false AS is_other FROM {table}_ranked WHERE rank <= 50
      UNION ALL SELECT NULL, NULL, sum(rows)::BIGINT, 51, true
      FROM {table}_ranked WHERE rank > 50 HAVING count(*) > 0''')
    cursor = con.execute(f'SELECT * FROM {table}_display ORDER BY rank')
    rows = [dict(zip([c[0] for c in cursor.description], row)) for row in cursor.fetchall()]
    if sum(row['rows'] for row in rows) != expected_rows:
        raise InvestigationError('Field transition row-count identity failed')
    pairs = con.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
    return dict(status='passed', rows=rows, changed_rows=expected_rows, distinct_transitions=pairs,
                omitted_transitions=max(0, pairs-50), evidence=table + '_display',
                ranking='Changed row count descending, then source and destination values (nulls first); top 50 plus Other.')
