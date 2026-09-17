from pathlib import Path
import yaml


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
    except (OSError, yaml.YAMLError) as exc:
        raise InvestigationError(f"Cannot read configuration: {exc}") from exc
    fields(cfg, ["mode", "reference", "current", "key", "metric", "dimensions", "report"],
           ["mode", "reference", "current", "key", "metric"], "configuration")
    if cfg["mode"] != "snapshots":
        raise InvestigationError("Only mode: snapshots is supported")
    names(cfg["key"], "key", True)
    cfg.setdefault("dimensions", [])
    names(cfg["dimensions"], "dimensions")
    if set(cfg["key"]) & set(cfg["dimensions"]):
        raise InvestigationError("Key columns cannot be dimensions (keys are private by default)")
    metric = cfg["metric"]
    fields(metric, ["name", "aggregate", "column", "null_policy"], ["name", "aggregate"], "metric")
    if not isinstance(metric["name"], str) or not metric["name"]:
        raise InvestigationError("metric.name must be nonempty text")
    if metric["aggregate"] == "sum":
        if not isinstance(metric.get("column"), str) or not metric["column"] or metric.get("null_policy") != "error":
            raise InvestigationError("sum requires column and null_policy: error")
        if metric["column"] in cfg["key"]:
            raise InvestigationError("A key cannot be the metric column")
    elif metric["aggregate"] != "count" or "column" in metric or "null_policy" in metric:
        raise InvestigationError("Use sum with column/null_policy, or count without either")
    cfg.setdefault("report", {})
    fields(cfg["report"], ["include_raw_rows"], [], "report")
    cfg["report"].setdefault("include_raw_rows", False)
    if type(cfg["report"]["include_raw_rows"]) is not bool:
        raise InvestigationError("include_raw_rows must be boolean")
    for side in ("reference", "current"):
        value = cfg[side]
        if not isinstance(value, str) or "://" in value or value.startswith(("//", "\\\\")):
            raise InvestigationError(f"{side} must be a local file path")
        resolved = (path.parent / value).resolve()
        if not resolved.is_file() or resolved.suffix.lower() not in (".csv", ".parquet"):
            raise InvestigationError(f"{side}: expected an existing CSV or Parquet file: {resolved}")
        cfg[side] = str(resolved)
    return cfg
