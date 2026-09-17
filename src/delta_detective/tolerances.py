"""Shared field predicates, including exact scaled-integer tolerance arithmetic."""
from decimal import Decimal
import re


def numeric_type(typ):
    return typ in {'TINYINT', 'SMALLINT', 'INTEGER', 'BIGINT', 'HUGEINT', 'UTINYINT',
                   'USMALLINT', 'UINTEGER', 'UBIGINT', 'UHUGEINT', 'FLOAT', 'DOUBLE'} or bool(
                       re.fullmatch(r'DECIMAL\(\d+,\d+\)', typ))


def field_conditions(before, after, typ, absolute=None):
    distinct = f'{before} IS DISTINCT FROM {after}'
    exact = f'{before} IS NOT DISTINCT FROM {after}'
    changed = distinct
    within = 'false'
    if absolute is not None:
        if typ in ('FLOAT', 'DOUBLE'):
            beyond = f"abs(CAST({after} AS DOUBLE) - CAST({before} AS DOUBLE)) > CAST('{absolute}' AS DOUBLE)"
        else:
            # Decimal strings have a fixed scale. Pad to a common scale and use
            # arbitrary-width integers so subtraction cannot overflow DECIMAL/HUGEINT.
            match = re.fullmatch(r'DECIMAL\(\d+,(\d+)\)', typ)
            field_scale = int(match[1]) if match else 0
            bound = Decimal(absolute)
            bound_scale = max(0, -bound.as_tuple().exponent)
            scale = max(field_scale, bound_scale)
            def scaled(column):
                return (f"CAST(replace(CAST({column} AS VARCHAR), '.', '') || "
                        f"repeat('0', {scale - field_scale}) AS BIGNUM)")
            a, b = scaled(before), scaled(after)
            integer_bound = format(bound, 'f').replace('.', '') + '0' * (scale - bound_scale)
            limit = f"CAST('{integer_bound}' AS BIGNUM)"
            beyond = f'(({b} - {a}) > {limit} OR ({a} - {b}) > {limit})'
        changed = f'({distinct}) AND ({before} IS NULL OR {after} IS NULL OR ({beyond}))'
        within = f'({distinct}) AND {before} IS NOT NULL AND {after} IS NOT NULL AND NOT ({beyond})'
    return {
        'changed_rows': changed,
        'unchanged_rows': f'NOT ({changed})',
        'exact_match_rows': exact,
        'within_tolerance_rows': within,
        'became_null': f'{before} IS NOT NULL AND {after} IS NULL',
        'from_null': f'{before} IS NULL AND {after} IS NOT NULL',
        'both_null': f'{before} IS NULL AND {after} IS NULL',
        'value_changed_rows': f'{before} IS NOT NULL AND {after} IS NOT NULL AND ({changed})',
        'became_blank': f"{after} = '' AND {before} IS DISTINCT FROM ''",
        'from_blank': f"{before} = '' AND {after} IS DISTINCT FROM ''",
        'increased_rows': f'{after} > {before} AND ({changed})',
        'decreased_rows': f'{after} < {before} AND ({changed})',
    }


def conditions_for(cfg, name, before, after, typ):
    return field_conditions(before, after, typ, cfg.get('field_tolerances', {}).get(name, {}).get('absolute'))


def any_field_changed(cfg, schema):
    return ' OR '.join('(' + conditions_for(cfg, name, f'rf{i}', f'cf{i}', schema[name])['changed_rows'] + ')'
                       for i, name in enumerate(cfg['compare_fields'])) or 'false'
