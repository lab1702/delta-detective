"""Interactive configuration authoring; source values never leave DuckDB."""
import os
import re
import tempfile
from pathlib import Path

import duckdb
import yaml

from .comparison import compare
from .config import InvestigationError, RULE_MEASURES, load_config, local_input, validate_rules
from .loading import fingerprint, ident, load_snapshots, validate_loaded
from .wizard_options import advanced_options, advanced_rule, yes_no, positive_limit
from .schema import check_schema
from .wizard_inputs import csv_options, map_columns, scoped_filters


def choose(prompt, options, ask, tell, *, minimum=0, maximum=None):
    for i, label in enumerate(options, 1):
        tell(f"  {i}. {label}")
    while True:
        answer = ask(prompt).strip()
        try:
            selected = [] if not answer else [int(part.strip()) - 1 for part in answer.split(',')]
            if (len(selected) < minimum or (maximum is not None and len(selected) > maximum)
                    or len(set(selected)) != len(selected)
                    or any(i < 0 or i >= len(options) for i in selected)):
                raise ValueError
            return selected
        except ValueError:
            tell("Choose distinct menu numbers separated by commas, within the displayed range.")


def key_valid(con, columns):
    nulls = ' OR '.join(f'{ident(c)} IS NULL' for c in columns)
    keys = ', '.join(map(ident, columns))
    return all(not con.execute(f"SELECT EXISTS (SELECT 1 FROM {side} WHERE {nulls}) OR "
                              f"EXISTS (SELECT 1 FROM {side} GROUP BY {keys} HAVING count(*) > 1)").fetchone()[0]
               for side in ('reference', 'current'))


def init_config(reference, current, out='comparison.yaml', *, ask=None, tell=print):
    ask = input if ask is None else ask
    paths = {side: local_input(value if isinstance(value, list) else str(value), Path.cwd(), side)
             for side, value in [('reference', reference), ('current', current)]}
    target = Path(out).absolute()
    if target.exists() or target.is_symlink():
        raise InvestigationError("Configuration output already exists; choose a new file")
    for value in paths.values():
        if isinstance(value, str) and Path(value).is_dir() and Path(value) in target.resolve().parents:
            raise InvestigationError('Configuration output must be outside input snapshot directories')
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with duckdb.connect() as con:
            before = {s: fingerprint(p) for s, p in paths.items()}
            prepare = yes_no('Configure input parsing, column mappings, or filters? [y/N]: ', ask, tell)
            input_options = csv_options(paths, ask, tell, choose) if prepare else {}
            tell("Inspecting both complete snapshots. No source row values will be displayed.")
            profiles = load_snapshots(con, {**paths, **input_options}, con.execute)
            if prepare:
                input_options.update(map_columns(con, profiles, ask, tell, choose))
                input_options.update(scoped_filters(con, profiles, ask, tell, choose))
            schemas = [profiles[s]['schema'] for s in ('reference', 'current')]
            common = {c: t for c, t in schemas[0].items() if schemas[1].get(c) == t}
            tell("Columns (reference / current types):")
            for c in dict.fromkeys([*schemas[0], *schemas[1]]):
                tell(f"  {c!r}: {schemas[0].get(c, 'missing')} / {schemas[1].get(c, 'missing')}")
            keys = [c for c, t in common.items() if t not in ('FLOAT', 'DOUBLE')
                    and not any(x in t for x in ('[', 'STRUCT', 'MAP', 'UNION'))]
            if not keys:
                raise InvestigationError("No compatible exact scalar columns available for keys")
            candidates = {c: key_valid(con, [c]) for c in keys}
            tell("Key candidates: unique/non-null checks cover the selected rows in both files, but do not establish identity.")
            tell("Select the columns that identify the same logical record in both snapshots. Empty snapshots provide no identity evidence.")
            while True:
                selected = choose("Key column numbers (required; comma-separated for composite key): ",
                                  [f"{c!r} ({common[c]}) - " + ('unique and non-null' if candidates[c] else 'not a valid single key; may be part of a composite') for c in keys],
                                  ask, tell, minimum=1)
                key = [keys[i] for i in selected]
                if key_valid(con, key):
                    break
                tell("That key has null components or duplicate tuples. Select another key or cancel with Ctrl+C.")
            numeric = []
            exact_numeric = {'TINYINT', 'SMALLINT', 'INTEGER', 'BIGINT', 'HUGEINT', 'UTINYINT', 'USMALLINT', 'UINTEGER', 'UBIGINT', 'UHUGEINT'}
            for c, t in common.items():
                if c in key or not (t in exact_numeric | {'FLOAT', 'DOUBLE'} or t.startswith('DECIMAL(')):
                    continue
                valid = all(not con.execute(f"SELECT EXISTS (SELECT 1 FROM {side} WHERE {ident(c)} IS NULL OR NOT isfinite({ident(c)}))").fetchone()[0]
                            for side in ('reference', 'current'))
                if valid:
                    numeric.append(c)
                else:
                    tell(f"Sum unavailable for {c!r}: null or nonfinite values.")
            selection = choose("Metric numbers (required; choose one or more): ", ['COUNT(*)'] + [f'SUM({c!r})' for c in numeric], ask, tell, minimum=1)
            metrics = []
            for i in selection:
                proposed = 'rows' if i == 0 else numeric[i-1]
                while True:
                    name = ask(f"Metric name [{proposed}]: ").strip() or proposed
                    if name not in [m['name'] for m in metrics]:
                        break
                    tell("Metric names must be unique.")
                metrics.append(dict(name=name, aggregate='count') if i == 0 else
                               dict(name=name, aggregate='sum', column=numeric[i-1], null_policy='error'))
            eligible = [c for c, t in common.items() if c not in key and
                        (t in exact_numeric | {'VARCHAR', 'BOOLEAN'} or re.fullmatch(r'DECIMAL\(\d+,\d+\)', t))]
            dimensions = []
            groups = []
            if eligible:
                dimensions = [eligible[i] for i in choose("Dimension numbers (Enter for none): ", [repr(c) for c in eligible], ask, tell)]
            if len(eligible) >= 2:
                while True:
                    indices = choose("Combined group numbers (at least two; Enter to finish): ", [repr(c) for c in eligible], ask, tell)
                    if not indices:
                        break
                    group = [eligible[i] for i in indices]
                    if len(group) < 2 or any(set(group) == set(g) for g in groups):
                        tell("Choose at least two columns and a group not already added.")
                    else:
                        groups.append(group)
            advanced = yes_no("Configure schema, field comparisons, scoped rules, or exports? [y/N]: ", ask, tell)
            options = advanced_options(con, schemas, common, key, ask, tell, choose) if advanced else {}
            compared = options.get('compare_fields', [])
            rules = []
            tell("Optional threshold rules use inclusive bounds; percentage bounds use 5 for 5%. Undefined percentages do not pass.")
            while True:
                name = ask("Rule name (Enter to finish): ").strip()
                if not name:
                    break
                if advanced:
                    candidate = advanced_rule(name, metrics, dimensions, groups, compared, common, ask, tell, choose)
                else:
                    metric_index = choose("Metric number: ", [m['name'] for m in metrics], ask, tell, minimum=1, maximum=1)[0]
                    measures = sorted(RULE_MEASURES)
                    measure = measures[choose("Measure number: ", measures, ask, tell, minimum=1, maximum=1)[0]]
                    candidate = dict(name=name, metric=metrics[metric_index]['name'], measure=measure)
                for bound in ('min', 'max'):
                    value = ask(f"{bound} (Enter for unbounded): ").strip()
                    if value:
                        candidate[bound] = value  # Preserve decimal input exactly in YAML.
                try:
                    validate_rules(rules + [candidate], {m['name'] for m in metrics}, [[d] for d in dimensions] + groups, compared)
                except InvestigationError as exc:
                    tell(str(exc) + "; please enter the rule again.")
                else:
                    if advanced and yes_no("Export supporting raw records if this rule fails? [y/N]: ", ask, tell):
                        candidate['export'] = {'limit': positive_limit(ask, tell)}
                    rules.append(candidate)
            cfg = dict(mode='snapshots', **paths, key=key, metrics=metrics, dimensions=dimensions,
                       dimension_groups=groups, rules=rules, report={'include_raw_rows': False})
            cfg.update(options)
            cfg.update(input_options)
            # Validate the exact saved configuration and run arithmetic checks against
            # the already loaded snapshots. Only publish once every metric passes.
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', suffix='.yaml',
                                             prefix='.delta-init-', dir=target.parent, delete=False) as stream:
                temp = Path(stream.name)
                yaml.safe_dump(cfg, stream, sort_keys=False, allow_unicode=True)
            cfg = load_config(temp)
            contract = check_schema(cfg.get('schema'), profiles)
            if contract['status'] == 'failed':
                tell("Schema contract does not match the current inputs. Investigating this configuration will produce a schema-only report (exit 4).")
            if contract['status'] != 'failed':
                validate_loaded(con, cfg, profiles, con.execute)
                tell("Validating selected metrics and breakdowns...")
                for i, metric in enumerate(metrics):
                    con.execute(f'CREATE SCHEMA wizard_{i}')
                    con.execute(f"SET schema = 'wizard_{i}'")
                    for side in paths:
                        con.execute(f'CREATE VIEW {side} AS SELECT * FROM main.{side}')
                    compare(con, dict(cfg, metric=metric), profiles, con.execute)
            if any(before[s] != profiles[s]['sha256'] or before[s] != fingerprint(paths[s]) for s in paths):
                raise InvestigationError("Input changed during setup; rerun with stable snapshots")
            # A same-directory hard link publishes a complete file without replacing
            # an existing file, including one created while prompts were open.
            os.link(temp, target)
        enabled = cfg['report']['include_raw_rows'] or cfg['report']['evidence_exports'] or any('export' in r for r in rules)
        tell(f"Configuration written: {target}. Raw exports are {'enabled as selected' if enabled else 'disabled'}.")
        tell(f'Next: delta-detective investigate "{target}" --out investigation')
        return target
    except (EOFError, KeyboardInterrupt):
        raise InvestigationError("Setup cancelled; no configuration was written") from None
    except duckdb.Error:
        raise InvestigationError("Could not inspect or validate snapshots; check file structure, selected types, and numeric overflow. No configuration was written.") from None
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)
