import hashlib
import re
from pathlib import Path
from .config import InvestigationError, configured_metrics, selected_dimensions, FIELD_PERCENTAGES


def ident(value):
    return '"' + value.replace('"', '""') + '"'


def literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def fingerprint(path):
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def load_snapshots(con, cfg, execute):
    profiles = {}
    for side in ("reference", "current"):
        path = cfg[side]
        parsing = {"format": Path(path).suffix[1:], "hive_partitioning": False}
        if Path(path).suffix.lower() == ".csv":
            # Capture the actual sniffer's decisions; materialize with explicit settings.
            overrides = cfg.get("csv", {}).get(side, {})
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
        execute(f"CREATE TABLE {side} AS SELECT * FROM {source}")
        schema = {r[0]: r[1] for r in con.execute(f"DESCRIBE {side}").fetchall()}
        profiles[side] = {"schema": schema, "sha256": fingerprint(path), "parsing": parsing}
    return profiles


def load_and_validate(con, cfg, execute):
    profiles = load_snapshots(con, cfg, execute)
    validate_loaded(con, cfg, profiles, execute)
    return profiles


def validate_loaded(con, cfg, profiles, execute):
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
