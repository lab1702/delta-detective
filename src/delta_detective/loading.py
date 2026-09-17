import hashlib
import json
import re
from pathlib import Path
from .config import InvestigationError, configured_metrics, selected_dimensions, FIELD_PERCENTAGES, identifier_key
from .tolerances import numeric_type
from .inputs import snapshot_files


def ident(value):
    return '"' + value.replace('"', '""') + '"'


def literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def file_fingerprint(path):
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def snapshot_digest(value, files):
    if not isinstance(value, list) and not Path(value).is_dir():
        return files[0]['sha256']
    inventory = [{k: f[k] for k in ('path', 'partitions', 'sha256')} for f in files]
    return hashlib.sha256(json.dumps(inventory, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()


def fingerprint(value):
    files = [dict(path=path, partitions=parts, sha256=file_fingerprint(path))
             for path, parts in snapshot_files(value)]
    return snapshot_digest(value, files)


def read_source(con, path, overrides, side):
    parsing = {"format": Path(path).suffix[1:], "hive_partitioning": False}
    if Path(path).suffix.lower() == ".csv":
        # Capture the actual sniffer's decisions; materialize with explicit settings.
        sniff_settings = []
        for name, value in overrides.items():
            if name == "types":
                rendered = "{" + ",".join(f"{literal(k)}:{literal(v)}" for k, v in value.items()) + "}"
            elif name == "header":
                rendered = str(value).lower()
            else:
                rendered = literal(value)
            sniff_settings.append(f"{'delim' if name == 'delimiter' else name}={rendered}")
        extra = ", " + ", ".join(sniff_settings) if sniff_settings else ""
        cursor = con.execute(f"SELECT * FROM sniff_csv({literal(path)}, sample_size=-1{extra})")
        sniff = dict(zip([c[0] for c in cursor.description], cursor.fetchone()))
        cols = sniff["Columns"]
        missing = set(overrides.get("types", {})) - {c["name"] for c in cols}
        if missing:
            raise InvestigationError(f"{side}: CSV type overrides name unknown columns {sorted(missing)}")
        options = {"delim": sniff["Delimiter"], "quote": sniff["Quote"], "escape": sniff["Escape"],
                   "dateformat": sniff["DateFormat"],
                   "timestampformat": sniff["TimestampFormat"]}
        for option in ("quote", "escape"):
            if options[option] == "(empty)":
                options[option] = ""
        # Explicit settings take precedence even if the sniffer does not need them.
        for name, value in overrides.items():
            if name not in ("types", "header", "nullstr"):
                options["delim" if name == "delimiter" else name] = value
        header = overrides.get("header", sniff["HasHeader"])
        nullstr = overrides.get("nullstr", "")
        if sniff["SkipRows"]:
            raise InvestigationError(f"{side}: CSV inference would skip leading rows; supply a clean, consistent CSV with no preamble")
        settings = [f"{k}={literal(v)}" for k, v in options.items() if v is not None]
        settings += [f"header={str(header).lower()}", f"skip={sniff['SkipRows']}",
                     "auto_detect=false", "hive_partitioning=false", "strict_mode=true", "ignore_errors=false", "null_padding=false",
                     f"nullstr={literal(nullstr)}", "comment=''", "columns={" + ",".join(f"{literal(c['name'])}:{literal(c['type'])}" for c in cols) + "}"]
        source = f"read_csv({literal(path)}, {', '.join(settings)})"
        parsing.update({"sniffer": sniff, "sample_size": -1, "strict_mode": True,
                        "ignore_errors": False, "null_padding": False, "nullstr": nullstr,
                        "overrides": overrides,
                        "effective": {**options, "header": header, "nullstr": nullstr,
                                      "columns": {c["name"]: c["type"] for c in cols}},
                        "newline_reader": "DuckDB default newline recognition; sniffer observation recorded separately",
                        "comment": ""})
    else:
        source = f"read_parquet({literal(path)}, hive_partitioning=false)"
    return source, parsing


def load_snapshots(con, cfg, execute):
    profiles = {}
    for side in ("reference", "current"):
        value = cfg[side]
        inventory = snapshot_files(value)
        files = []
        expected = None
        input_table = f'{side}_input_files'
        execute(f'CREATE TABLE {input_table} (file_index BIGINT, path VARCHAR, row_count BIGINT)')
        for index, (path, partitions) in enumerate(inventory):
            source, parsing = read_source(con, path, cfg.get('csv', {}).get(side, {}), side)
            part = f'{side}_input_part'
            execute(f'CREATE TEMP TABLE {part} AS SELECT * FROM {source}')
            physical = {r[0]: r[1] for r in con.execute(f'DESCRIBE {part}').fetchall()}
            additions = []
            for name, partition_value in partitions.items():
                colliding = [c for c in physical if identifier_key(c) == identifier_key(name)]
                if colliding:
                    if colliding != [name]:
                        raise InvestigationError(f'{side}: Hive partition name collides with a differently cased source column {name!r}')
                    # Never silently replace a physical column with its directory value.
                    expected_value = 'NULL' if partition_value is None else literal(partition_value)
                    execute(f"SELECT CASE WHEN EXISTS (SELECT 1 FROM {part} WHERE CAST({ident(name)} AS VARCHAR) IS DISTINCT FROM {expected_value}) THEN error('Hive partition conflicts with stored column') ELSE true END")
                else:
                    rendered = 'NULL' if partition_value is None else literal(partition_value)
                    additions.append(f'CAST({rendered} AS VARCHAR) AS {ident(name)}')
            projection = '*' + (', ' + ', '.join(additions) if additions else '')
            effective = dict(physical)
            effective.update({name: 'VARCHAR' for name in partitions if name not in physical})
            if expected is None:
                expected = effective
                execute(f'CREATE TABLE {side} AS SELECT {projection} FROM {part}')
            else:
                if effective != expected:
                    raise InvestigationError(f'{side}: files must have identical column names and types; incompatible schema in {path!r}')
                # Reorder by names, never coerce or pad missing columns.
                cols = ', '.join(map(ident, expected))
                execute(f'INSERT INTO {side} SELECT {cols} FROM (SELECT {projection} FROM {part})')
            execute(f'INSERT INTO {input_table} SELECT {index}, {literal(path)}, count(*) FROM {part}')
            rows = con.execute(f'SELECT row_count FROM {input_table} WHERE file_index={index}').fetchone()[0]
            execute(f'DROP TABLE {part}')
            files.append(dict(path=path, sha256=file_fingerprint(path), row_count=rows,
                              partitions=partitions, schema=physical, parsing=parsing))
        source_schema = expected
        mapping = cfg.get('column_mapping', {}).get(side, {})
        if mapping:
            missing = set(mapping) - source_schema.keys()
            if missing:
                raise InvestigationError(f"{side}: column_mapping names unknown source columns {sorted(missing)}")
            targets = [mapping.get(name, name) for name in source_schema]
            normalized = [identifier_key(name) for name in targets]
            if len(normalized) != len(set(normalized)):
                raise InvestigationError(f"{side}: column_mapping causes a column name collision (case-insensitive)")
            projection = ', '.join(f'{ident(name)} AS {ident(target)}' for name, target in zip(source_schema, targets))
            execute(f'CREATE OR REPLACE TABLE {side} AS SELECT {projection} FROM {side}')
        schema = {r[0]: r[1] for r in con.execute(f'DESCRIBE {side}').fetchall()}
        directory = not isinstance(value, list) and Path(value).is_dir()
        parsing = files[0]['parsing'] if len(files) == 1 and not directory else dict(
            format='multi_file', hive_partitioning=bool(inventory[0][1]),
            partition_types='Virtual Hive columns are VARCHAR; physical columns retain their types.',
            schema_policy='Identical effective column names and types; columns aligned by name.')
        profiles[side] = dict(schema=schema, source_schema=source_schema, column_mapping=mapping,
                              sha256=snapshot_digest(value, files), parsing=parsing, files=files,
                              file_count=len(files), row_count=sum(f['row_count'] for f in files),
                              input_kind='directory' if directory else 'files' if isinstance(value, list) else 'file',
                              file_evidence=input_table)
    return profiles


def load_and_validate(con, cfg, execute):
    profiles = load_snapshots(con, cfg, execute)
    validate_loaded(con, cfg, profiles, execute)
    return profiles


def validate_loaded(con, cfg, profiles, execute):
    for name in cfg.get('field_transitions', []):
        for side in profiles:
            if profiles[side]['schema'].get(name) not in ('VARCHAR', 'BOOLEAN'):
                raise InvestigationError(f'Field transitions require a text or boolean comparison field: {name!r}')
    for name in cfg.get('field_tolerances', {}):
        for side in profiles:
            if not numeric_type(profiles[side]['schema'].get(name, '')):
                raise InvestigationError(f'Field tolerance requires a numeric comparison field: {name!r}')
    metrics = configured_metrics(cfg)
    required = cfg["key"] + selected_dimensions(cfg) + cfg.get('compare_fields', []) + [m["column"] for m in metrics if m["aggregate"] == "sum"]
    for side in ("reference", "current"):
        schema = profiles[side]["schema"]
        missing = set(required) - schema.keys()
        if missing:
            raise InvestigationError(f"{side}: missing required columns {sorted(missing)}; check CSV header and inferred schema")
        nulls = " OR ".join(f"{ident(k)} IS NULL" for k in cfg["key"])
        keys = ",".join(map(ident, cfg["key"]))
        execute(f"SELECT CASE WHEN EXISTS (SELECT 1 FROM {side} WHERE {nulls}) THEN error('{side}: null key component') ELSE true END")
        execute(f"SELECT CASE WHEN EXISTS (SELECT 1 FROM {side} GROUP BY {keys} HAVING count(*)>1) THEN error('{side}: duplicate keys') ELSE true END")
        for metric in metrics:
            if metric["aggregate"] == "sum":
                col = metric["column"]
                typ = schema[col]
                numeric = typ in ("TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT", "UHUGEINT", "FLOAT", "DOUBLE") or typ.startswith("DECIMAL(")
                if not numeric:
                    raise InvestigationError(f"{side}: sum column must be numeric; inferred {typ}. Use explicit CSV types or typed Parquet for exact decimals; repair malformed CSV values upstream.")
                execute(f"SELECT CASE WHEN EXISTS (SELECT 1 FROM {side} WHERE {ident(col)} IS NULL OR NOT isfinite({ident(col)})) THEN error('{side}: null or nonfinite metric') ELSE true END")
    for col in required:
        a, b = (profiles[s]["schema"][col] for s in ("reference", "current"))
        if a != b:
            raise InvestigationError(f"Incompatible selected column types for {col!r}: {a} / {b}. Supply matching explicit CSV types or typed Parquet; no implicit coercion is performed.")
    for col in cfg["key"]:
        typ = profiles["reference"]["schema"][col]
        if typ in ("FLOAT", "DOUBLE") or any(x in typ for x in ("[", "STRUCT", "MAP", "UNION")):
            raise InvestigationError(f"Unsupported key type {typ}; use exact scalar keys")
    for col in selected_dimensions(cfg):
        typ = profiles["reference"]["schema"][col]
        scalar_types = {"VARCHAR", "BOOLEAN", "TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT",
                        "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT", "UHUGEINT"}
        if typ not in scalar_types and not re.fullmatch(r"DECIMAL\(\d+,\d+\)", typ):
            raise InvestigationError(f"Dimension {col!r} must be categorical text, boolean, or exact numeric")
    for col in cfg.get('compare_fields', []):
        typ = profiles['reference']['schema'][col]
        if not comparison_field_type(typ):
            raise InvestigationError(f'Unsupported compare_fields type for {col!r}: {typ}; use text, boolean, numeric, date, or time values')
        if typ in ('FLOAT', 'DOUBLE'):
            for side in ('reference', 'current'):
                execute(f"SELECT CASE WHEN EXISTS (SELECT 1 FROM {side} WHERE NOT isfinite({ident(col)})) THEN error('{side}: nonfinite comparison field') ELSE true END")
    for rule in cfg.get('rules', []):
        if 'field' not in rule:
            continue
        typ = profiles['reference']['schema'][rule['field']]
        count = FIELD_PERCENTAGES.get(rule['measure'], rule['measure'])
        if ((count in ('became_blank', 'from_blank') and typ != 'VARCHAR')
                or (count in ('increased_rows', 'decreased_rows') and typ in ('VARCHAR', 'BOOLEAN'))):
            raise InvestigationError(f"Field rule measure {rule['measure']!r} is not supported for type {typ}")
    return profiles


def comparison_field_type(typ):
    return typ in {'VARCHAR', 'BOOLEAN', 'TINYINT', 'SMALLINT', 'INTEGER', 'BIGINT', 'HUGEINT',
                   'UTINYINT', 'USMALLINT', 'UINTEGER', 'UBIGINT', 'UHUGEINT', 'FLOAT', 'DOUBLE',
                   'DATE', 'TIME', 'TIME WITH TIME ZONE', 'TIMESTAMP', 'TIMESTAMP_S', 'TIMESTAMP_MS',
                   'TIMESTAMP_NS', 'TIMESTAMP WITH TIME ZONE'} or bool(re.fullmatch(r'DECIMAL\(\d+,\d+\)', typ))
