"""Standalone input diagnostics; raw evidence stays in DuckDB unless requested."""
import json
import shutil
import tempfile
from pathlib import Path

import duckdb
from jinja2 import Environment, BaseLoader

from .config import InvestigationError, load_config, configured_metrics, selected_dimensions, FIELD_PERCENTAGES
from .core import prepare_output, publish
from .loading import load_snapshots, validate_loaded, fingerprint, ident, literal, comparison_field_type
from .filtering import apply_filters
from .schema import check_schema
from .tolerances import numeric_type
from .findings import dumps


TEMPLATE = '''<!doctype html><html lang="en"><meta charset="utf-8">
<title>Input validation</title><style>body{font:16px/1.5 system-ui;max-width:1100px;margin:40px auto;padding:0 24px}table{border-collapse:collapse;width:100%}td,th{padding:8px;border:1px solid #ccc;text-align:left}pre{white-space:pre-wrap;overflow-wrap:anywhere}</style>
<h1>Input validation: {{ data.status }}</h1><p>No comparison, metric reconciliation, or threshold evaluation was performed. Passing input checks does not guarantee arithmetic reconciliation will succeed.</p>
<h2>Population scope</h2><p>{{ data.filter_scope.note }}</p>
{% for side, counts in data.filter_scope.inputs.items() %}<p>{{ side }}: {{ counts.included_rows }} included; {{ counts.excluded_rows }} excluded; {{ counts.total_rows }} total.</p>{% endfor %}
<h2>Diagnostics</h2><p>Counts can overlap; do not sum across checks. Duplicate counts include all rows in repeated key groups, including null-key groups.</p>
<table><tr><th>Input</th><th>Check</th><th>Columns</th><th>Status</th><th>Details</th></tr>
{% for c in data.checks %}<tr><td>{{ c.side }}</td><td>{{ c.check }}</td><td>{{ c.columns|join(', ') }}</td><td>{{ c.status }}</td><td>{{ c.details }}</td></tr>{% endfor %}</table>
<h2>Evidence exports</h2><p>Disabled unless explicitly requested. Enabled exports contain keys and offending selected fields, with original values.</p>
{% for e in data.exports %}<p><a href="{{ e.file }}">{{ e.file }}</a>: {{ e.rows }} of {{ e.total_rows }} rows; limit {{ e.limit }}; truncated {{ e.truncated }}.</p>{% else %}<p>No raw evidence exported.</p>{% endfor %}
<h2>Replay SQL</h2><pre>{{ sql }}</pre></html>'''


def validate(config, out, overwrite=False, export_invalid_rows=False, limit=100):
    if type(limit) is not int or limit <= 0:
        raise InvestigationError('Evidence limit must be a positive integer')
    cfg = load_config(config)
    target = Path(out).resolve()
    if target.is_dir() and any(target.iterdir()) and overwrite:
        try:
            previous = json.loads((target / 'manifest.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            previous = {}
        if not isinstance(previous, dict) or previous.get('kind') != 'validation':
            raise InvestigationError('Validation output must be separate from investigation bundles; choose another directory')
    target = prepare_output(target, overwrite, [config, cfg['reference'], cfg['current']])
    stage = Path(tempfile.mkdtemp(prefix='.delta-validation-', dir=target.parent))
    statements, checks, exports = [], [], []
    try:
        with duckdb.connect() as con:
            def execute(sql):
                con.execute(sql)
                statements.append(sql + ';')

            def record(side, check, columns, failed, details, predicate=None, total=0):
                index = len(checks)
                checks.append(dict(side=side, check=check, columns=columns,
                                   status='failed' if failed else 'passed', details=details))
                if not (failed and predicate and export_invalid_rows and total):
                    return
                schema = profiles[side]['schema']
                selected = list(dict.fromkeys(c for c in cfg['key'] + columns if c in schema))
                names = ', '.join(map(ident, selected))
                view = f'validation_evidence_{index}'
                execute(f'CREATE VIEW {view} AS SELECT {names} FROM {side} WHERE {predicate} ORDER BY ALL LIMIT {limit}')
                file = f'{side}_{index}_invalid_rows.csv'
                con.execute(f'COPY {view} TO {literal(stage / file)} (HEADER, FORMAT CSV)')
                exports.append(dict(file=file, check=check, side=side, columns=selected, rows=min(total, limit),
                                    total_rows=total, limit=limit, truncated=total > limit, evidence=view))

            def counted(side, check, columns, predicate):
                table = f'validation_check_{len(checks)}'
                execute(f'CREATE TABLE {table} AS SELECT count(*) AS affected_rows FROM {side} WHERE {predicate}')
                n = con.execute(f'SELECT affected_rows FROM {table}').fetchone()[0]
                record(side, check, columns, n > 0, dict(affected_rows=n, evidence=table), predicate, n)

            before = {s: fingerprint(cfg[s]) for s in ('reference', 'current')}
            profiles = load_snapshots(con, cfg, execute)
            contract = check_schema(cfg.get('schema'), profiles)
            for item in contract['results']:
                record(item['side'], 'schema_contract', [item['column']], item['status'] == 'failed',
                       {k: item[k] for k in ('issue', 'expected_type', 'observed_type')})
            scope = dict(status='not_evaluated', inputs={}, filters=cfg.get('filters', []),
                         note='Schema contract failed; population filters and row checks were not evaluated.')
            if contract['status'] != 'failed':
                try:
                    scope = apply_filters(con, cfg, profiles, execute)
                except InvestigationError as exc:
                    record('both', 'filter_configuration', [], True, str(exc))
                    scope['note'] = 'Invalid filters; row checks were not evaluated.'
                else:
                    if scope['status'] == 'not_configured':
                        for side in profiles:
                            n = con.execute(f'SELECT count(*) FROM {side}').fetchone()[0]
                            scope['inputs'][side] = dict(total_rows=n, included_rows=n, excluded_rows=0)
                    required = list(dict.fromkeys(cfg['key'] + selected_dimensions(cfg) + cfg['compare_fields'] +
                        [m['column'] for m in configured_metrics(cfg) if m['aggregate'] == 'sum']))
                    for column in required:
                        types = [profiles[s]['schema'].get(column) for s in profiles]
                        record('both', 'selected_column', [column], None in types or types[0] != types[1],
                               dict(reference_type=types[0], current_type=types[1]))
                    for side, profile in profiles.items():
                        schema = profile['schema']
                        for column in required:
                            if column not in schema:
                                continue
                            typ = schema[column]
                            valid = True
                            if column in cfg['key']:
                                valid &= typ not in ('FLOAT', 'DOUBLE') and not any(x in typ for x in ('[', 'STRUCT', 'MAP', 'UNION'))
                            if column in selected_dimensions(cfg):
                                valid &= typ in ('VARCHAR', 'BOOLEAN') or (numeric_type(typ) and typ not in ('FLOAT', 'DOUBLE'))
                            if column in cfg['compare_fields']:
                                valid &= comparison_field_type(typ)
                            if column in cfg.get('field_tolerances', {}):
                                valid &= numeric_type(typ)
                            if column in cfg.get('field_transitions', []):
                                valid &= typ in ('VARCHAR', 'BOOLEAN')
                            metric = any(m.get('column') == column for m in configured_metrics(cfg))
                            if metric:
                                valid &= numeric_type(typ)
                            record(side, 'column_type', [column], not valid, dict(observed_type=typ))
                            if metric and numeric_type(typ):
                                counted(side, 'null_metric', [column], f'{ident(column)} IS NULL')
                                counted(side, 'nonfinite_metric', [column], f'NOT isfinite({ident(column)})')
                            elif column in cfg['compare_fields'] and typ in ('FLOAT', 'DOUBLE'):
                                counted(side, 'nonfinite_comparison_field', [column], f'NOT isfinite({ident(column)})')
                        if all(k in schema for k in cfg['key']):
                            counted(side, 'null_key', cfg['key'], ' OR '.join(f'{ident(k)} IS NULL' for k in cfg['key']))
                        key_usable = all(k in schema and schema[k] not in ('FLOAT', 'DOUBLE') and not any(
                            x in schema[k] for x in ('[', 'STRUCT', 'MAP', 'UNION')) for k in cfg['key'])
                        if key_usable:
                            keys = ', '.join(map(ident, cfg['key']))
                            projected = ', '.join(f'{ident(k)} AS k{i}' for i, k in enumerate(cfg['key']))
                            group = f'{side}_duplicate_keys'
                            execute(f'CREATE TABLE {group} AS SELECT {projected}, count(*) AS validation_group_rows FROM {side} GROUP BY {keys} HAVING count(*) > 1')
                            groups, rows = con.execute(f'SELECT count(*), coalesce(sum(validation_group_rows),0) FROM {group}').fetchone()
                            predicate = 'EXISTS (SELECT 1 FROM ' + group + ' d WHERE ' + ' AND '.join(
                                f'{side}.{ident(k)} IS NOT DISTINCT FROM d.k{i}' for i, k in enumerate(cfg['key'])) + ')'
                            record(side, 'duplicate_key', cfg['key'], groups > 0,
                                   dict(duplicate_groups=groups, affected_rows=rows, excess_rows=rows-groups, evidence=group), predicate, rows)
                        for rule in cfg['rules']:
                            if 'field' in rule and rule['field'] in schema:
                                typ = schema[rule['field']]
                                measure = FIELD_PERCENTAGES.get(rule['measure'], rule['measure'])
                                invalid = ((measure in ('became_blank', 'from_blank') and typ != 'VARCHAR') or
                                           (measure in ('increased_rows', 'decreased_rows') and typ in ('VARCHAR', 'BOOLEAN')))
                                record(side, 'field_rule_type', [rule['field']], invalid,
                                       dict(rule=rule['name'], measure=rule['measure'], observed_type=typ))
                    # Canonical validation also checks field-rule applicability and
                    # guards against divergence as investigation validation evolves.
                    if not any(c['status'] == 'failed' for c in checks):
                        try:
                            validate_loaded(con, cfg, profiles, execute)
                        except InvestigationError as exc:
                            record('both', 'investigation_validation', [], True, str(exc))
            if any(before[s] != profiles[s]['sha256'] or before[s] != fingerprint(cfg[s]) for s in profiles):
                raise InvestigationError('Input changed during validation; rerun with stable snapshots')
            data = dict(kind='validation', status='failed' if any(c['status'] == 'failed' for c in checks) else 'passed',
                        checks=checks, filter_scope=scope, schema_checks=contract, exports=exports)
            sql = '-- Validation only; run in a fresh DuckDB database with matching inputs.\n' + '\n\n'.join(statements)
            manifest = dict(data, configuration=cfg, inputs=profiles, duckdb_version=duckdb.__version__)
            html = Environment(loader=BaseLoader(), autoescape=True).from_string(TEMPLATE).render(data=data, sql=sql)
            for name, content in [('validation.json', dumps(data)), ('manifest.json', dumps(manifest)),
                                  ('analysis.sql', sql), ('report.html', html)]:
                (stage / name).write_text(content, encoding='utf-8')
        publish(stage, target)
        return data
    except duckdb.Error:
        raise InvestigationError('Could not load or validate snapshots; check file structure, parsing options, and selected types. No validation bundle was written.') from None
    finally:
        if stage.exists():
            shutil.rmtree(stage)
