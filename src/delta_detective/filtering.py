"""Apply a shared, typed population scope without accepting SQL configuration."""
from datetime import date

from .config import InvestigationError, threshold_number
from .loading import ident, literal


SCOPE_NOTE = ('All comparisons, rules, and exports describe the selected subset. '
              'Records entering or leaving this subset appear as additions or removals, '
              'even if their keys exist in both source files. Key and value validation '
              'applies to included rows; the complete files are still parsed.')
NUMERIC = {'TINYINT', 'SMALLINT', 'INTEGER', 'BIGINT', 'HUGEINT',
           'UTINYINT', 'USMALLINT', 'UINTEGER', 'UBIGINT', 'UHUGEINT', 'FLOAT', 'DOUBLE'}
BOUNDS = {'gt': '>', 'gte': '>=', 'lt': '<', 'lte': '<='}


def value_sql(value, typ, column):
    error = f'Filter value does not match type {typ} for column {column!r}'
    if typ == 'VARCHAR' and type(value) is str:
        return literal(value)
    if typ == 'BOOLEAN' and type(value) is bool:
        return str(value).lower()
    if typ == 'DATE' and type(value) is str:
        try:
            parsed = date.fromisoformat(value)
            if parsed.isoformat() != value:
                raise ValueError()
        except ValueError:
            raise InvestigationError(error + '; use a quoted YYYY-MM-DD date') from None
        return f'CAST({literal(value)} AS DATE)'
    if (typ in NUMERIC or typ.startswith('DECIMAL(')) and type(value) in (str, int, float):
        number = threshold_number(value)
        # Bound precision before formatting; do not round a bound to the column's scale.
        scale = max(0, -number.as_tuple().exponent)
        precision = max(1, max(number.adjusted() + 1, 0) + scale)
        if precision > 38:
            raise InvestigationError('Numeric filter values must fit DECIMAL precision 38; quote exact decimals')
        return f'CAST({literal(format(number, "f"))} AS DECIMAL({precision},{scale}))'
    raise InvestigationError(error)


def filter_predicate(filters, profiles):
    predicates = []
    for item in filters:
        column, op = item['column'], item['operator']
        types = [profiles[s]['schema'].get(column) for s in ('reference', 'current')]
        if None in types:
            raise InvestigationError(f'Filter column {column!r} must exist in both mapped schemas')
        if types[0] != types[1]:
            raise InvestigationError(f'Filter column {column!r} must have matching types in both snapshots')
        typ, col = types[0], ident(column)
        if op in ('is_null', 'is_not_null'):
            predicates.append(f'{col} IS ' + ('NOT ' if op == 'is_not_null' else '') + 'NULL')
            continue
        if op in BOUNDS and typ not in NUMERIC and not typ.startswith('DECIMAL(') and typ != 'DATE':
            raise InvestigationError('Filter bounds require a numeric or DATE column')
        values = item['value'] if op == 'in' else [item['value']]
        rendered = [value_sql(v, typ, column) for v in values]
        if op == 'in':
            predicates.append(f'{col} IN ({", ".join(rendered)})')
        else:
            predicates.append(f'{col} {BOUNDS.get(op, "=")} {rendered[0]}')
    return ' AND '.join(f'({p})' for p in predicates) or 'true'


def apply_filters(con, cfg, profiles, execute):
    filters = cfg.get('filters', [])
    scope = dict(status='applied' if filters else 'not_configured', filters=filters, inputs={},
                 note=SCOPE_NOTE if filters else 'Full snapshots compared.')
    if not filters:
        return scope
    predicate = filter_predicate(filters, profiles)
    for side in ('reference', 'current'):
        table = f'{side}_filter_scope'
        execute(f'CREATE TABLE {table} AS SELECT count(*) AS total_rows, '
                f'count(*) FILTER (WHERE {predicate}) AS included_rows FROM {side}')
        total, included = con.execute(f'SELECT total_rows, included_rows FROM {table}').fetchone()
        scope['inputs'][side] = dict(total_rows=total, included_rows=included,
                                     excluded_rows=total - included, evidence=f'analysis.sql: main.{table}')
        execute(f'DELETE FROM {side} WHERE ({predicate}) IS NOT TRUE')
    return scope
