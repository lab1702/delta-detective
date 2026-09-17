"""Optional guided configuration for contracts, fields, scopes, and exports."""
import json
from decimal import Decimal

import duckdb

from .config import FIELD_COUNTS, FIELD_PERCENTAGES, RULE_MEASURES, InvestigationError, threshold_number
from .loading import comparison_field_type, ident


def yes_no(prompt, ask, tell):
    while True:
        value = ask(prompt).strip().lower()
        if value in ('', 'n', 'no'):
            return False
        if value in ('y', 'yes'):
            return True
        tell('Enter yes or no; Enter means no.')


def positive_limit(ask, tell):
    while True:
        value = ask('Export row limit [100]: ').strip() or '100'
        try:
            limit = int(value)
            if limit > 0:
                return limit
        except ValueError:
            pass
        tell('Enter a positive whole number.')


def expected_type(column, default, ask, tell):
    while True:
        value = ask(f'Expected type for {column!r} [{default}] (* for presence only): ').strip() or default
        if value == '*':
            return None
        try:
            return str(duckdb.sqltype(value))
        except (duckdb.Error, ValueError):
            tell('Enter a valid DuckDB type, such as VARCHAR or DECIMAL(18,2), or *.')


def advanced_options(con, schemas, common, key, ask, tell, choose):
    options = {}
    if yes_no('Declare a schema contract? [y/N]: ', ask, tell):
        columns = list(dict.fromkeys([*schemas[0], *schemas[1]]))
        selected = choose('Required column numbers (choose at least one): ', [repr(c) for c in columns], ask, tell, minimum=1)
        contract = {columns[i]: expected_type(columns[i], schemas[0].get(columns[i], schemas[1].get(columns[i])), ask, tell) for i in selected}
        while True:
            column = ask('Additional required column name (Enter to finish): ')
            if not column:
                break
            if column in contract:
                tell('That column is already included.')
                continue
            contract[column] = expected_type(column, '*', ask, tell)
        strict = yes_no('Reject columns not listed in the contract? [y/N]: ', ask, tell)
        options['schema'] = dict(columns=contract, allow_extra_columns=not strict)
    eligible = []
    for c, typ in common.items():
        if c in key or not comparison_field_type(typ):
            continue
        if typ in ('FLOAT', 'DOUBLE') and any(con.execute(
                f'SELECT EXISTS (SELECT 1 FROM {side} WHERE NOT isfinite({ident(c)}))').fetchone()[0]
                for side in ('reference', 'current')):
            tell(f'Field comparison unavailable for {c!r}: nonfinite values.')
            continue
        eligible.append(c)
    compared = [eligible[i] for i in choose('Compared field numbers (Enter for none): ',
                [f'{c!r} ({common[c]})' for c in eligible], ask, tell)] if eligible else []
    options['compare_fields'] = compared
    tell('Raw exports contain record keys and selected before/after values. Leave export selections blank to keep them disabled.')
    kinds = ['all', 'added', 'removed', 'changed', 'moved', 'largest_changes'] + (['field_changed'] if compared else [])
    selected = choose('Raw export numbers (Enter for none): ', kinds, ask, tell)
    report = {'include_raw_rows': False, 'evidence_exports': []}
    for i in selected:
        kind = kinds[i]
        if kind == 'all':
            report['include_raw_rows'] = True
        else:
            tell(f'Configure {kind} export:')
            report['evidence_exports'].append(dict(kind=kind, limit=positive_limit(ask, tell)))
    options['report'] = report
    return options


def selector_value(column, typ, ask, tell):
    while True:
        text = ask(f'Segment value for {column!r} ({typ}; JSON scalar, e.g. "West", 12, true, null): ')
        try:
            value = json.loads(text, parse_float=Decimal)
            if value is None:
                return None
            if typ == 'VARCHAR' and isinstance(value, str):
                return value
            if typ == 'BOOLEAN' and type(value) is bool:
                return value
            if typ not in ('VARCHAR', 'BOOLEAN') and type(value) in (int, Decimal, str):
                threshold_number(str(value))
                return str(value)  # Preserve exact numeric category values.
        except (ValueError, InvestigationError):
            pass
        tell('Enter a JSON scalar matching the column type, or null for a missing category.')


def advanced_rule(name, metrics, dimensions, groups, compared, common, ask, tell, choose):
    scopes = ['Overall metric']
    available_groups = [[d] for d in dimensions] + groups
    if available_groups:
        scopes.append('Segment metric')
    if compared:
        scopes.append('Compared field')
    scope = scopes[choose('Rule target number: ', scopes, ask, tell, minimum=1, maximum=1)[0]]
    candidate = {'name': name}
    if scope == 'Compared field':
        field = compared[choose('Field number: ', [repr(c) for c in compared], ask, tell, minimum=1, maximum=1)[0]]
        candidate['field'] = field
        typ = common[field]
        allowed = FIELD_COUNTS.copy()
        if typ != 'VARCHAR':
            allowed -= {'became_blank', 'from_blank'}
        if typ in ('VARCHAR', 'BOOLEAN'):
            allowed -= {'increased_rows', 'decreased_rows'}
        measures = sorted(allowed | {m for m, count in FIELD_PERCENTAGES.items() if count in allowed})
        tell('Field percentages use all matched records as the denominator; added/removed keys are excluded.')
    else:
        candidate['metric'] = metrics[choose('Metric number: ', [m['name'] for m in metrics], ask, tell, minimum=1, maximum=1)[0]]['name']
        measures = sorted(RULE_MEASURES)
        if scope == 'Segment metric':
            group = available_groups[choose('Dimension/group number: ', [repr(g) for g in available_groups], ask, tell, minimum=1, maximum=1)[0]]
            candidate['group_by'] = group
            tell('Segment removals include keys that disappeared or moved out of the segment.')
            if yes_no('Restrict this rule to one exact segment? [y/N]: ', ask, tell):
                candidate['where'] = {c: selector_value(c, common[c], ask, tell) for c in group}
    candidate['measure'] = measures[choose('Measure number: ', measures, ask, tell, minimum=1, maximum=1)[0]]
    return candidate
