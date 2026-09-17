import hashlib
import re
from pathlib import Path
from .config import InvestigationError


def ident(value):
    return '"' + value.replace('"', '""') + '"'


def literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def fingerprint(path):
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def load_and_validate(con, cfg, execute):
    profiles = {}
    for side in ("reference", "current"):
        path = cfg[side]
        parsing = {"format": Path(path).suffix[1:]}
        if Path(path).suffix.lower() == ".csv":
            # Capture the actual sniffer's decisions; materialize with explicit settings.
            cursor = con.execute(f"SELECT * FROM sniff_csv({literal(path)}, sample_size=-1)")
            sniff = dict(zip([c[0] for c in cursor.description], cursor.fetchone()))
            cols = sniff["Columns"]
            options = {"delim": sniff["Delimiter"], "quote": sniff["Quote"], "escape": sniff["Escape"],
                       "dateformat": sniff["DateFormat"],
                       "timestampformat": sniff["TimestampFormat"]}
            for option in ("quote", "escape"):
                if options[option] == "(empty)":
                    options[option] = ""
            if sniff["SkipRows"]:
                raise InvestigationError(f"{side}: CSV inference would skip leading rows; supply a clean, consistent CSV with no preamble")
            settings = [f"{k}={literal(v)}" for k, v in options.items() if v is not None]
            settings += [f"header={str(sniff['HasHeader']).lower()}", f"skip={sniff['SkipRows']}",
                         "auto_detect=false", "strict_mode=true", "ignore_errors=false", "null_padding=false",
                         "nullstr=''", "comment=''", "columns={" + ",".join(f"{literal(c['name'])}:{literal(c['type'])}" for c in cols) + "}"]
            source = f"read_csv({literal(path)}, {', '.join(settings)})"
            parsing.update({"sniffer": sniff, "sample_size": -1, "strict_mode": True,
                            "ignore_errors": False, "null_padding": False, "nullstr": "",
                            "newline_reader": "DuckDB default newline recognition; sniffer observation recorded separately",
                            "comment": ""})
        else:
            source = f"read_parquet({literal(path)})"
        execute(f"CREATE TABLE {side} AS SELECT * FROM {source}")
        schema = {r[0]: r[1] for r in con.execute(f"DESCRIBE {side}").fetchall()}
        required = cfg["key"] + cfg["dimensions"] + ([cfg["metric"]["column"]] if cfg["metric"]["aggregate"] == "sum" else [])
        missing = set(required) - schema.keys()
        if missing:
            raise InvestigationError(f"{side}: missing required columns {sorted(missing)}; check CSV header and inferred schema")
        nulls = " OR ".join(f"{ident(k)} IS NULL" for k in cfg["key"])
        keys = ",".join(map(ident, cfg["key"]))
        execute(f"SELECT CASE WHEN EXISTS (SELECT 1 FROM {side} WHERE {nulls}) THEN error('{side}: null key component') ELSE true END")
        execute(f"SELECT CASE WHEN EXISTS (SELECT 1 FROM {side} GROUP BY {keys} HAVING count(*)>1) THEN error('{side}: duplicate keys') ELSE true END")
        if cfg["metric"]["aggregate"] == "sum":
            col = cfg["metric"]["column"]
            typ = schema[col]
            numeric = typ in ("TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT", "UHUGEINT", "FLOAT", "DOUBLE") or typ.startswith("DECIMAL(")
            if not numeric:
                raise InvestigationError(f"{side}: sum column must be numeric; inferred {typ}. Use typed Parquet for exact decimals; repair malformed CSV values upstream.")
            execute(f"SELECT CASE WHEN EXISTS (SELECT 1 FROM {side} WHERE {ident(col)} IS NULL OR NOT isfinite({ident(col)})) THEN error('{side}: null or nonfinite metric') ELSE true END")
        profiles[side] = {"schema": schema, "sha256": fingerprint(path), "parsing": parsing}
    for col in cfg["key"] + cfg["dimensions"] + ([cfg["metric"]["column"]] if cfg["metric"]["aggregate"] == "sum" else []):
        a, b = (profiles[s]["schema"][col] for s in ("reference", "current"))
        if a != b:
            raise InvestigationError(f"Incompatible selected column types for {col!r}: {a} / {b}. Supply matching explicit types in Parquet; no implicit coercion is performed.")
    for col in cfg["key"]:
        typ = profiles["reference"]["schema"][col]
        if typ in ("FLOAT", "DOUBLE") or any(x in typ for x in ("[", "STRUCT", "MAP", "UNION")):
            raise InvestigationError(f"Unsupported key type {typ}; use exact scalar keys")
    for col in cfg["dimensions"]:
        typ = profiles["reference"]["schema"][col]
        scalar_types = {"VARCHAR", "BOOLEAN", "TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT",
                        "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT", "UHUGEINT"}
        if typ not in scalar_types and not re.fullmatch(r"DECIMAL\(\d+,\d+\)", typ):
            raise InvestigationError(f"Dimension {col!r} must be categorical text, boolean, or exact numeric")
    return profiles
