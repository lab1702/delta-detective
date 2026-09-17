"""Optional input preparation before choosing comparison keys and metrics."""
import json
from decimal import Decimal

from .config import (InvestigationError, validate_csv_options, validate_column_mapping,
                     validate_filters, identifier_key)
from .filtering import apply_filters, filter_predicate, NUMERIC, SCOPE_NOTE
from .loading import ident
from .inputs import has_csv


def csv_options(paths, ask, tell, choose):
    result = {}
    options = ['types', 'delimiter', 'header', 'quote', 'escape', 'nullstr', 'dateformat', 'timestampformat']
    tell('CSV overrides are optional. Type overrides use exact source names; unspecified settings use inference.')
    for side, path in paths.items():
        if not has_csv(path):
            continue
        settings = {}
        while True:
            selected = choose(f'{side} CSV option (Enter to finish): ', options, ask, tell, maximum=1)
            if not selected:
                break
            name = options[selected[0]]
            try:
                if name == 'types':
                    column = ask('Source column name (headerless files use column0, column1, etc.): ')
                    typ = ask('DuckDB type (e.g. VARCHAR or DECIMAL(18,2)): ')
                    value = {**settings.get('types', {}), column: typ}
                elif name == 'header':
                    value = json.loads(ask('Header present (true or false): '))
                else:
                    value = json.loads(ask(f'{name} as a JSON string (e.g. ";", "", "\\t", "%d/%m/%Y"): '))
                candidate = {**settings, name: value}
                validate_csv_options({**paths, 'csv': {side: candidate}})
            except (ValueError, InvestigationError) as exc:
                tell(f'Invalid CSV setting: {exc}. Please choose the option again.')
            else:
                settings = candidate
        if settings:
            result[side] = settings
    return {'csv': result} if result else {}


def map_columns(con, profiles, ask, tell, choose):
    mappings = {}
    for side, profile in profiles.items():
        columns = list(profile['schema'])
        while True:
            selected = choose(f'{side} columns to rename (Enter for none): ',
                              [f'{c!r} ({profile["schema"][c]})' for c in columns], ask, tell)
            mapping = {columns[i]: ask(f'Logical name for {columns[i]!r}: ') for i in selected}
            try:
                validate_column_mapping({'column_mapping': {side: mapping}})
                targets = [mapping.get(c, c) for c in columns]
                keys = [identifier_key(c) for c in targets]
                if len(set(keys)) != len(keys):
                    raise InvestigationError('Targets collide with another mapped or unchanged column')
            except InvestigationError as exc:
                tell(f'{exc}. Please enter the mapping for {side} again.')
                continue
            break
        if mapping:
            projection = ', '.join(f'{ident(c)} AS {ident(t)}' for c, t in zip(columns, targets))
            con.execute(f'CREATE OR REPLACE TABLE {side} AS SELECT {projection} FROM {side}')
            profile['schema'] = dict(zip(targets, profile['schema'].values()))
            profile['column_mapping'] = mapping
            mappings[side] = mapping
    return {'column_mapping': mappings} if mappings else {}


def scoped_filters(con, profiles, ask, tell, choose):
    common = {c: t for c, t in profiles['reference']['schema'].items()
              if profiles['current']['schema'].get(c) == t}
    columns = list(common)
    filters = []
    tell('Optional filters use mapped names and AND semantics. No source category values are listed.')
    tell(SCOPE_NOTE)
    while columns:
        selected = choose('Filter column (Enter to finish): ',
                          [f'{c!r} ({common[c]})' for c in columns], ask, tell, maximum=1)
        if not selected:
            break
        column = columns[selected[0]]
        typ = common[column]
        numeric = typ in NUMERIC or typ.startswith('DECIMAL(')
        operators = ['is_null', 'is_not_null']
        if numeric or typ in ('VARCHAR', 'BOOLEAN', 'DATE'):
            operators = ['equals', 'in'] + (['gt', 'gte', 'lt', 'lte'] if numeric or typ == 'DATE' else []) + operators
        op = operators[choose('Filter operator: ', operators, ask, tell, minimum=1, maximum=1)[0]]
        candidate = dict(column=column, operator=op)
        while True:
            try:
                if op not in ('is_null', 'is_not_null'):
                    value = json.loads(ask('Filter value (JSON scalar or membership list; quote exact decimals and ISO dates): '), parse_float=Decimal)
                    if numeric:
                        value = [str(v) if isinstance(v, Decimal) else v for v in value] if isinstance(value, list) else str(value) if isinstance(value, Decimal) else value
                    candidate['value'] = value
                validate_filters([candidate])
                predicate = filter_predicate(filters + [candidate], profiles)
            except (ValueError, InvestigationError) as exc:
                tell(f'Invalid filter: {exc}. Please enter the value again.')
                continue
            break
        filters.append(candidate)
        included_counts = []
        for side in profiles:
            total, included = con.execute(f'SELECT count(*), count(*) FILTER (WHERE {predicate}) FROM {side}').fetchone()
            included_counts.append(included)
            tell(f'{side} preview: {included} included, {total - included} excluded, {total} total rows.')
        if not any(included_counts):
            tell('Both selected populations are empty; they provide no evidence for record identity.')
    if filters:
        apply_filters(con, {'filters': filters}, profiles, con.execute)
    return {'filters': filters} if filters else {}
