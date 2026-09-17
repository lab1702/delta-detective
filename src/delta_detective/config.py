from pathlib import Path
from decimal import Decimal, InvalidOperation
import yaml
import duckdb


class InvestigationError(ValueError):
    pass


class UniqueLoader(yaml.SafeLoader):
    pass


def _mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if not isinstance(key, str) or key in result:
            raise InvestigationError("Configuration keys must be unique strings")
        result[key] = loader.construct_object(value_node)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def fields(value, allowed, required, label):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        raise InvestigationError(f"Invalid {label}: allowed fields {allowed}; required {required}")


def names(value, label, nonempty=False):
    if (not isinstance(value, list) or (nonempty and not value)
            or any(not isinstance(x, str) or not x for x in value)
            or len(set(value)) != len(value)):
        raise InvestigationError(f"{label} must be a list of unique, nonempty column names")


def load_config(path):
    path = Path(path).resolve()
    try:
        cfg = yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueLoader)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise InvestigationError(f"Cannot read configuration: {exc}") from exc
    fields(cfg, ["mode", "reference", "current", "key", "metric", "metrics", "dimensions", "dimension_groups", "report", "rules", "schema", "compare_fields", "csv", "column_mapping", "filters"],
           ["mode", "reference", "current", "key"], "configuration")
    if cfg["mode"] != "snapshots":
        raise InvestigationError("Only mode: snapshots is supported")
    if 'schema' in cfg:
        contract = cfg['schema']
        fields(contract, ['columns', 'allow_extra_columns'], ['columns'], 'schema contract')
        if not isinstance(contract['columns'], dict) or not contract['columns']:
            raise InvestigationError('schema.columns must be a nonempty mapping of required columns to types or null')
        contract.setdefault('allow_extra_columns', True)
        if type(contract['allow_extra_columns']) is not bool:
            raise InvestigationError('schema.allow_extra_columns must be boolean')
        for column, typ in contract['columns'].items():
            if not isinstance(column, str) or not column:
                raise InvestigationError('Schema column names must be nonempty strings')
            if typ is None:
                continue
            if not isinstance(typ, str) or not typ.strip():
                raise InvestigationError('Schema types must be DuckDB type strings or null')
            try:
                contract['columns'][column] = str(duckdb.sqltype(typ))
            except (duckdb.Error, ValueError):
                raise InvestigationError(f'Invalid schema type for column {column!r}') from None
    names(cfg["key"], "key", True)
    cfg.setdefault('compare_fields', [])
    names(cfg['compare_fields'], 'compare_fields')
    if set(cfg['key']) & set(cfg['compare_fields']):
        raise InvestigationError('Key columns cannot be compare_fields')
    cfg.setdefault("dimensions", [])
    names(cfg["dimensions"], "dimensions")
    cfg.setdefault("dimension_groups", [])
    groups = cfg["dimension_groups"]
    if not isinstance(groups, list):
        raise InvestigationError("dimension_groups must be a list of column lists")
    for group in groups:
        names(group, "dimension group", True)
        if len(group) < 2:
            raise InvestigationError("Each dimension group requires at least two columns")
    if len({frozenset(g) for g in groups}) != len(groups):
        raise InvestigationError("Duplicate dimension groups")
    if set(cfg["key"]) & set(selected_dimensions(cfg)):
        raise InvestigationError("Key columns cannot be dimensions (keys are private by default)")
    if ("metric" in cfg) == ("metrics" in cfg):
        raise InvestigationError("Specify exactly one of metric or metrics")
    metrics = cfg.get("metrics", [cfg.get("metric")])
    if not isinstance(metrics, list) or not metrics:
        raise InvestigationError("metrics must be a nonempty list")
    for metric in metrics:
        validate_metric(metric, cfg["key"])
    if len({m["name"] for m in metrics}) != len(metrics):
        raise InvestigationError("Metric names must be unique")
    validate_rules(cfg.setdefault("rules", []), {m["name"] for m in metrics},
                   [[d] for d in cfg["dimensions"]] + cfg["dimension_groups"], cfg['compare_fields'])
    cfg.setdefault("report", {})
    fields(cfg["report"], ["include_raw_rows", "evidence_exports"], [], "report")
    cfg["report"].setdefault("include_raw_rows", False)
    if type(cfg["report"]["include_raw_rows"]) is not bool:
        raise InvestigationError("include_raw_rows must be boolean")
    exports = cfg["report"].setdefault("evidence_exports", [])
    if not isinstance(exports, list):
        raise InvestigationError("evidence_exports must be a list")
    kinds = set()
    for export in exports:
        fields(export, ["kind", "limit"], ["kind"], "evidence export")
        kind = export["kind"]
        if not isinstance(kind, str) or kind not in ("added", "removed", "changed", "moved", "largest_changes", "field_changed") or kind in kinds:
            raise InvestigationError("Evidence export kinds must be unique: added, removed, changed, moved, largest_changes, field_changed")
        if kind == 'field_changed' and not cfg['compare_fields']:
            raise InvestigationError('field_changed export requires compare_fields')
        kinds.add(kind)
        if kind == "largest_changes":
            export.setdefault("limit", 100)
        if "limit" in export and (type(export["limit"]) is not int or export["limit"] <= 0):
            raise InvestigationError("Evidence export limit must be a positive integer")
    for side in ("reference", "current"):
        cfg[side] = local_input(cfg[side], path.parent, side)
    validate_csv_options(cfg)
    validate_column_mapping(cfg)
    validate_filters(cfg.get('filters', []))
    return cfg


def validate_filters(filters):
    if not isinstance(filters, list):
        raise InvestigationError('filters must be a list')
    for item in filters:
        fields(item, ['column', 'operator', 'value'], ['column', 'operator'], 'filter')
        if not isinstance(item['column'], str) or not item['column'] or '\x00' in item['column']:
            raise InvestigationError('Filter column must be nonempty text without NUL')
        op = item['operator']
        if not isinstance(op, str) or op not in ('equals', 'in', 'gt', 'gte', 'lt', 'lte', 'is_null', 'is_not_null'):
            raise InvestigationError('Unsupported filter operator')
        if op in ('is_null', 'is_not_null'):
            if 'value' in item:
                raise InvestigationError('Null filters must omit value')
            continue
        if 'value' not in item:
            raise InvestigationError('Filter requires value')
        values = item['value'] if op == 'in' else [item['value']]
        if not isinstance(values, list) or not values:
            raise InvestigationError('Membership filters require a nonempty value list')
        if any(type(v) not in (str, bool, int, float) or (isinstance(v, str) and '\x00' in v) for v in values):
            raise InvestigationError('Filter values must be text, booleans, or finite numbers; use is_null for nulls')
        for v in values:
            if type(v) in (int, float):
                threshold_number(v)


def identifier_key(name):
    # DuckDB identifiers use ASCII case-insensitive comparison, even when quoted.
    return name.translate(str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"))


def validate_column_mapping(cfg):
    mappings = cfg.get("column_mapping", {})
    fields(mappings, ["reference", "current"], [], "column_mapping")
    for side, mapping in mappings.items():
        if not isinstance(mapping, dict):
            raise InvestigationError(f"column_mapping.{side} must map source names to logical names")
        for source, target in mapping.items():
            if any(not isinstance(n, str) or not n or "\x00" in n for n in (source, target)):
                raise InvestigationError(f"column_mapping.{side} names must be nonempty strings without NUL")
        targets = [identifier_key(n) for n in mapping.values()]
        if len(targets) != len(set(targets)):
            raise InvestigationError(f"column_mapping.{side} has colliding target names (case-insensitive)")


def validate_csv_options(cfg):
    if "csv" not in cfg:
        return
    fields(cfg["csv"], ["reference", "current"], [], "csv")
    allowed = ["types", "delimiter", "header", "quote", "escape", "nullstr", "dateformat", "timestampformat"]
    for side, options in cfg["csv"].items():
        fields(options, allowed, [], f"csv.{side}")
        if Path(cfg[side]).suffix.lower() != ".csv":
            raise InvestigationError(f"csv.{side} requires a CSV input")
        for name, value in options.items():
            label = f"csv.{side}.{name}"
            if name == "types":
                if not isinstance(value, dict) or not value:
                    raise InvestigationError(f"{label} must be a nonempty mapping of columns to DuckDB types")
                for column, typ in value.items():
                    if not column or not isinstance(typ, str) or not typ.strip():
                        raise InvestigationError(f"{label} requires nonempty column names and type strings")
                    try:
                        value[column] = str(duckdb.sqltype(typ))
                    except (duckdb.Error, ValueError):
                        raise InvestigationError(f"Invalid CSV type for column {column!r}") from None
            elif name == "header":
                if type(value) is not bool:
                    raise InvestigationError(f"{label} must be boolean")
            else:
                if not isinstance(value, str) or "\x00" in value or "\n" in value or "\r" in value:
                    raise InvestigationError(f"{label} must be text without NUL or newline characters")
                if name in ("delimiter", "quote", "escape"):
                    minimum = 1 if name == "delimiter" else 0
                    if not minimum <= len(value.encode("utf-8")) <= 1:
                        raise InvestigationError(f"{label} must be one ASCII character" + (" or empty" if minimum == 0 else ""))
                elif name in ("dateformat", "timestampformat") and not value:
                    raise InvestigationError(f"{label} must be nonempty")


def configured_metrics(cfg):
    return cfg["metrics"] if "metrics" in cfg else [cfg["metric"]]


def selected_dimensions(cfg):
    return list(dict.fromkeys(cfg["dimensions"] + [c for g in cfg.get("dimension_groups", []) for c in g]))


def validate_metric(metric, keys):
    fields(metric, ["name", "aggregate", "column", "null_policy"], ["name", "aggregate"], "metric")
    if not isinstance(metric["name"], str) or not metric["name"]:
        raise InvestigationError("metric.name must be nonempty text")
    if metric["aggregate"] == "sum":
        if not isinstance(metric.get("column"), str) or not metric["column"] or metric.get("null_policy") != "error":
            raise InvestigationError("sum requires column and null_policy: error")
        if metric["column"] in keys:
            raise InvestigationError("A key cannot be the metric column")
    elif metric["aggregate"] != "count" or "column" in metric or "null_policy" in metric:
        raise InvestigationError("Use sum with column/null_policy, or count without either")


RULE_MEASURES = {
    "delta", "abs_delta", "percent_change", "abs_percent_change",
    "current_total", "current_rows", "added_rows", "removed_rows", "removed_percent",
}


def threshold_number(value):
    if type(value) not in (int, float, str):
        raise InvestigationError("Rule bounds must be finite numbers or numeric strings")
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise InvestigationError("Rule bounds must be finite numbers or numeric strings") from None
    if not number.is_finite():
        raise InvestigationError("Rule bounds must be finite numbers or numeric strings")
    return number


FIELD_COUNTS = {'changed_rows', 'unchanged_rows', 'became_null', 'from_null', 'both_null',
                'value_changed_rows', 'became_blank', 'from_blank', 'increased_rows', 'decreased_rows'}
FIELD_PERCENTAGES = {'percent_' + name.removesuffix('_rows'): name for name in FIELD_COUNTS}


def validate_rules(rules, metric_names, groups=(), compare_fields=()):
    if not isinstance(rules, list):
        raise InvestigationError("rules must be a list")
    seen = set()
    for rule in rules:
        fields(rule, ["name", "metric", "field", "measure", "min", "max", "group_by", "where", "export"], ["name", "measure"], "rule")
        if 'export' in rule:
            fields(rule['export'], ['limit'], [], 'rule export')
            rule['export'].setdefault('limit', 100)
            if type(rule['export']['limit']) is not int or rule['export']['limit'] <= 0:
                raise InvestigationError('Rule export limit must be a positive integer')
        name = rule["name"]
        if not isinstance(name, str) or not name.strip() or name in seen:
            raise InvestigationError("Rule names must be unique, nonempty text")
        seen.add(name)
        if ('metric' in rule) == ('field' in rule):
            raise InvestigationError('Rule requires exactly one of metric or field')
        if 'field' in rule:
            if not isinstance(rule['field'], str) or rule['field'] not in compare_fields:
                raise InvestigationError('Rule field must name a configured compare_fields column')
            if 'group_by' in rule or 'where' in rule:
                raise InvestigationError('Field rules apply to all matched records; group_by and where are not supported')
            measures = FIELD_COUNTS | FIELD_PERCENTAGES.keys()
        else:
            if not isinstance(rule["metric"], str) or rule["metric"] not in metric_names:
                raise InvestigationError("Rule metric must name a configured metric")
            measures = RULE_MEASURES
        if not isinstance(rule["measure"], str) or rule["measure"] not in measures:
            raise InvestigationError(f"Rule measure must be one of {sorted(measures)}")
        bounds = {k: threshold_number(rule[k]) for k in ("min", "max") if k in rule}
        if not bounds or ("min" in bounds and "max" in bounds and bounds["min"] > bounds["max"]):
            raise InvestigationError("Rule requires min and/or max, with min <= max")
        if "group_by" in rule:
            names(rule["group_by"], "rule group_by", True)
            if rule["group_by"] not in groups:
                raise InvestigationError("Rule group_by must match a configured dimension or dimension group in order")
        if "where" in rule:
            if "group_by" not in rule or not isinstance(rule["where"], dict) or set(rule["where"]) != set(rule["group_by"]):
                raise InvestigationError("Rule where must specify exactly the group_by columns")
            for value in rule["where"].values():
                if value is not None and type(value) not in (str, int, float, bool):
                    raise InvestigationError("Segment selectors must be scalar values or null")
                if type(value) is float:
                    threshold_number(value)


def local_input(value, base, side):
    if not isinstance(value, str) or "://" in value or value.startswith(("//", "\\\\")):
        raise InvestigationError(f"{side} must be a local file path")
    if "\x00" in value:
        raise InvestigationError(f"{side}: input paths must not contain NUL characters")
    resolved = (Path(base) / value).resolve()
    if any(character in str(resolved) for character in '*?[]'):
        raise InvestigationError(f"{side}: input paths must not contain glob characters (* ? [ ]); rename the file or parent directory")
    if not resolved.is_file() or resolved.suffix.lower() not in (".csv", ".parquet"):
        raise InvestigationError(f"{side}: expected an existing CSV or Parquet file: {resolved}")
    return str(resolved)
