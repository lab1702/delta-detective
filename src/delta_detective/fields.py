"""Aggregate comparisons of selected non-key fields on matched records."""
from decimal import Decimal, localcontext

from .config import InvestigationError


def compare_fields(con, cfg, profiles, execute):
    fields = cfg.get('compare_fields', [])
    if not fields:
        return {'status': 'not_configured', 'fields': []}
    changed = ' OR '.join(f'rf{i} IS DISTINCT FROM cf{i}' for i in range(len(fields)))
    execute(f"""CREATE TABLE main.field_overview AS SELECT count(*) AS matched_rows,
      count(*) FILTER (WHERE {changed}) AS changed_rows
      FROM main.joined WHERE rp AND cp""")
    matched, any_changed = con.execute('SELECT * FROM main.field_overview').fetchone()
    results = []
    for i, name in enumerate(fields):
        typ = profiles['reference']['schema'][name]
        before, after = f'rf{i}', f'cf{i}'
        conditions = {
            'changed_rows': f'{before} IS DISTINCT FROM {after}',
            'unchanged_rows': f'{before} IS NOT DISTINCT FROM {after}',
            'became_null': f'{before} IS NOT NULL AND {after} IS NULL',
            'from_null': f'{before} IS NULL AND {after} IS NOT NULL',
            'both_null': f'{before} IS NULL AND {after} IS NULL',
            'value_changed_rows': f'{before} IS NOT NULL AND {after} IS NOT NULL AND {before} IS DISTINCT FROM {after}',
        }
        if typ == 'VARCHAR':
            conditions.update(became_blank=f"{after} = '' AND {before} IS DISTINCT FROM ''",
                              from_blank=f"{before} = '' AND {after} IS DISTINCT FROM ''")
        elif typ != 'BOOLEAN':
            conditions.update(increased_rows=f'{after} > {before}', decreased_rows=f'{after} < {before}')
        table = f'main.field_{i}'
        execute(f"CREATE TABLE {table} AS SELECT " + ', '.join(
            f'count(*) FILTER (WHERE {condition}) AS {label}' for label, condition in conditions.items())
            + ' FROM main.joined WHERE rp AND cp')
        cur = con.execute(f'SELECT * FROM {table}')
        counts = dict(zip([c[0] for c in cur.description], cur.fetchone()))
        if (counts['changed_rows'] + counts['unchanged_rows'] != matched
                or counts['changed_rows'] != counts['became_null'] + counts['from_null'] + counts['value_changed_rows']):
            raise InvestigationError('Field comparison row-count identity failed')
        with localcontext() as ctx:
            ctx.prec = 100
            percent = Decimal(counts['changed_rows']) * 100 / matched if matched else None
        results.append(dict(name=name, type=typ, matched_rows=matched, **counts,
                            percent_changed=percent, evidence=table))
    return dict(status='passed', matched_rows=matched, changed_rows=any_changed,
                unchanged_rows=matched-any_changed, fields=results, evidence='main.field_overview')
