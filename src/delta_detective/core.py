from pathlib import Path
import shutil
import tempfile
from datetime import datetime, timezone
import duckdb
from .config import load_config, InvestigationError, configured_metrics, selected_dimensions
from .loading import load_snapshots, validate_loaded, fingerprint, literal
from .comparison import compare
from .findings import build_findings, dumps, LIMITATIONS
from .report import render
from .rules import evaluate_rules
from .schema import check_schema
from .fields import compare_fields
from .tolerances import any_field_changed
from .rule_exports import export_rule_evidence
from .summary import build_summary
from .filtering import apply_filters, SCOPE_NOTE


def prepare_output(out, overwrite, inputs=()):
    out = Path(out).resolve()
    sources = [path for value in inputs for path in (value if isinstance(value, list) else [value])]
    for source in sources:
        source = Path(source).resolve()
        if source == out or out in source.parents:
            raise InvestigationError("Output directory must not contain configuration or input files")
        if source.is_dir() and source in out.parents:
            raise InvestigationError('Output must be outside input snapshot directories')
    if out.exists() and (not out.is_dir() or (any(out.iterdir()) and not overwrite)):
        raise InvestigationError("Output exists and is nonempty; choose another directory or use --overwrite")
    out.parent.mkdir(parents=True, exist_ok=True)
    return out


def investigate(config, out, overwrite=False):
    """Validate and compare a YAML config; return bounded findings and write a bundle.

    Work happens in a sibling staging directory. Failed investigations leave any
    previous output untouched. DuckDB streams an optional full selected-row export.
    """
    cfg = load_config(config)
    out = prepare_output(out, overwrite, [config, cfg["reference"], cfg["current"]])
    stage = Path(tempfile.mkdtemp(prefix=".delta-", dir=out.parent))
    con = duckdb.connect()
    statements = []
    def execute(sql):
        con.execute(sql)
        statements.append(sql + ";")
    try:
        before = {s: fingerprint(cfg[s]) for s in ("reference", "current")}
        profiles = load_snapshots(con, cfg, execute)
        schema_checks = check_schema(cfg.get('schema'), profiles)
        if schema_checks['status'] == 'failed':
            for side in profiles:
                if before[side] != profiles[side]['sha256'] or fingerprint(cfg[side]) != before[side]:
                    raise InvestigationError('Input changed during analysis; rerun with stable snapshots')
            return publish_schema_failure(stage, out, cfg, profiles, schema_checks, statements)
        filter_scope = apply_filters(con, cfg, profiles, execute)
        validate_loaded(con, cfg, profiles, execute)
        a, b = (profiles[s]["schema"] for s in ("reference", "current"))
        schema_changes = [{"column": c, "reference_type": a.get(c), "current_type": b.get(c)} for c in sorted(a.keys() | b.keys()) if a.get(c) != b.get(c)]
        results = []
        evidence = []
        metrics = configured_metrics(cfg)
        checks = ["Passed: required files/columns, null and duplicate keys, selected type compatibility, key-count identities, overall and independent dimension reconciliation for every metric.",
                  "Passed: numeric, finite, non-null values for all sum metrics (not applicable to COUNT(*)).",
                  "Ran: schema comparison (see below). Unrelated schema changes are informational.",
                  "Skipped: comparisons of nonselected row attributes; operational-cause investigation."]
        for index, metric in enumerate(metrics):
            # The first metric retains legacy table names in main. Each additional
            # metric has an isolated namespace and reads the same loaded snapshots.
            namespace = "main" if index == 0 else f"metric_{index}"
            if index:
                execute(f"CREATE SCHEMA {namespace}")
            execute(f"SET schema = '{namespace}'")
            # Explicit views avoid depending on DuckDB's search path for inputs.
            if index:
                for side in ("reference", "current"):
                    execute(f"CREATE VIEW {side} AS SELECT * FROM main.{side}")
            metric_cfg = dict(cfg, metric=metric)
            summary, dimensions, reclassifications = compare(con, metric_cfg, profiles, execute)
            if index == 0:
                field_changes = compare_fields(con, cfg, profiles, execute)
            item = build_findings(summary, dimensions, reclassifications, schema_changes)
            for finding in item["findings"]:
                finding["evidence"] = f"analysis.sql: {namespace}.reconciliation"
            for finding in dimensions + reclassifications:
                finding["evidence"] = namespace + "." + finding["evidence"]
            for finding in reclassifications:
                finding["movements"]["evidence"] = namespace + "." + finding["movements"]["evidence"]
            item.update(metric=metric, validation=checks, sql_schema=namespace)
            results.append(item)
            evidence.extend(export_evidence(con, execute, cfg, stage, index, len(metrics)))
        for side in profiles:
            if before[side] != profiles[side]["sha256"] or fingerprint(cfg[side]) != before[side]:
                raise InvestigationError("Input changed during analysis; rerun with stable snapshots")
        data = dict(results[0])  # Legacy top-level fields refer to the first metric.
        data["metrics"] = results
        data["evidence_exports"] = evidence
        data['field_changes'] = field_changes
        data['schema_checks'] = schema_checks
        data['execution_status'] = 'success'
        data['filter_scope'] = filter_scope
        data["rule_checks"] = evaluate_rules(cfg["rules"], results, con, cfg, execute, field_changes=field_changes)
        evidence.extend(export_rule_evidence(con, cfg, results, data['rule_checks'], stage, execute))
        data['investigation_summary'] = build_summary(data)
        summary = data["summary"]
        replay = []
        if cfg['compare_fields']:
            replay.append('SELECT * FROM main.field_overview;')
            replay.extend(f'SELECT * FROM main.field_{i};' for i in range(len(cfg['compare_fields'])))
            replay.extend(f'SELECT * FROM main.field_{i}_transitions_display ORDER BY rank;'
                          for i, name in enumerate(cfg['compare_fields']) if name in cfg.get('field_transitions', []))
        for item in results:
            ns = item["sql_schema"]
            replay.append(f"SELECT * FROM {ns}.reconciliation;")
            for i in range(len(item["dimensions"])):
                replay.append(f"SELECT * FROM {ns}.dimension_{i}_display ORDER BY rank; SELECT * FROM {ns}.reclassification_{i};")
                replay.append(f"SELECT * FROM {ns}.movement_{i}_display ORDER BY rank;")
        sql = "-- Run in a fresh DuckDB database; matching input files are required.\n" + "\n\n".join(statements + replay)
        manifest = {"application_version": "0.1.0", "duckdb_version": duckdb.__version__,
                    "created_utc": datetime.now(timezone.utc).isoformat(), "configuration": cfg,
                    "inputs": profiles, "execution_status": "success", "reconciliation": {k: summary[k] for k in ("status", "exact", "residual", "tolerance")},
                    "validation": checks, "assumptions_and_limitations": LIMITATIONS,
                    "rule_checks": data["rule_checks"],
                    "schema_checks": schema_checks,
                    "filter_scope": filter_scope,
                    "field_changes": field_changes,
                    'investigation_summary': data['investigation_summary'],
                    "metric_reconciliations": [{"name": item["metric"]["name"], "sql_schema": item["sql_schema"],
                        **{k: item["summary"][k] for k in ("status", "exact", "residual", "tolerance")}} for item in results],
                    "raw_evidence": {"included": bool(evidence), "exports": evidence, "selected_dimensions": selected_dimensions(cfg), 'compare_fields': cfg['compare_fields'],
                        "contents": "Per metric: kN keys in configured order; rv/cv metric values; rdN/cdN dimensions in selected_dimensions order; rfN/cfN compared fields in compare_fields order; rp/cp side presence. General focused exports also include contribution; rule exports do not. Only selected fields, not full source rows."}}
        for filename, content in [("findings.json", dumps(data)), ("manifest.json", dumps(manifest)),
                                  ("analysis.sql", sql), ("report.html", render(cfg, data, checks, sql, dumps({"changes": schema_changes, "inputs": profiles})))]:
            (stage / filename).write_text(content, encoding="utf-8")
        publish(stage, out)
        return data
    except duckdb.Error as exc:
        # Do not echo DuckDB's offending row/key values in default diagnostics.
        message = str(exc)
        if 'Hive partition conflicts with stored column' in message:
            raise InvestigationError('Hive partition conflicts with a stored column; directory values must agree with every row in that file') from None
        for reason in ("duplicate keys", "null key component", "null or nonfinite metric", "nonfinite comparison field"):
            if reason in message:
                side = "reference" if "reference:" in message else "current"
                raise InvestigationError(f"{side}: {reason}; repair the source snapshot and rerun") from None
        category = "numeric overflow" if "overflow" in message.lower() or "out of range" in message.lower() else "parse, type, or SQL execution error"
        raise InvestigationError(f"DuckDB {category}; verify CSV structure and parsing overrides, or supply explicitly typed Parquet. No successful bundle was written.") from None
    finally:
        con.close()
        if stage.exists():
            shutil.rmtree(stage)


def publish(stage, out):
    """Retain the previous bundle until the completed stage is in place."""
    backup = None
    if out.exists():
        backup = Path(tempfile.mkdtemp(prefix=".delta-backup-", dir=out.parent))
        backup.rmdir()
        out.replace(backup)
    try:
        stage.replace(out)
    except OSError:
        if backup is not None:
            backup.replace(out)
        raise
    if backup is not None:
        # Publication succeeded. A locked backup must not turn success into failure.
        shutil.rmtree(backup, ignore_errors=True)


def publish_schema_failure(stage, out, cfg, profiles, schema_checks, statements):
    checks = ['Schema contract failed. Comparison, threshold rules, and raw exports were not run.']
    data = dict(execution_status='schema_contract_failed', schema_checks=schema_checks,
                metrics=[], summary=None, findings=[], dimensions=[], reclassifications=[],
                evidence_exports=[], rule_checks={'status': 'not_evaluated', 'results': []},
                field_changes={'status': 'not_evaluated', 'fields': []},
                validation=checks, limitations=LIMITATIONS)
    data['filter_scope'] = dict(status='not_evaluated', filters=cfg.get('filters', []), inputs={},
                               note='Schema contract failed; filters were not applied. ' + SCOPE_NOTE)
    data['investigation_summary'] = build_summary(data)
    sql = '-- Schema-only run. Replay loads snapshots; Python evaluates the schema contract.\n' + '\n\n'.join(
        statements + ['DESCRIBE main.reference;', 'DESCRIBE main.current;'])
    manifest = dict(application_version='0.1.0', duckdb_version=duckdb.__version__,
                    created_utc=datetime.now(timezone.utc).isoformat(), configuration=cfg,
                    inputs=profiles, execution_status=data['execution_status'], schema_checks=schema_checks,
                    reconciliation=None, metric_reconciliations=[], rule_checks=data['rule_checks'],
                    field_changes=data['field_changes'],
                    filter_scope=data['filter_scope'],
                    investigation_summary=data['investigation_summary'],
                    validation=checks, raw_evidence={'included': False, 'exports': []},
                    assumptions_and_limitations=LIMITATIONS)
    for filename, content in [('findings.json', dumps(data)), ('manifest.json', dumps(manifest)),
                              ('analysis.sql', sql), ('report.html', render(cfg, data, checks, sql, dumps(profiles)))]:
        (stage / filename).write_text(content, encoding='utf-8')
    publish(stage, out)
    return data


def export_evidence(con, execute, cfg, stage, metric_index, metric_count):
    """Stream opt-in evidence; record reproducible selection views, never COPY paths."""
    exports = []
    prefix = "" if metric_count == 1 else f"metric_{metric_index}_"
    requests = ([{"kind": "all"}] if cfg["report"]["include_raw_rows"] else []) + cfg["report"]["evidence_exports"]
    for request in requests:
        kind = request["kind"]
        filename = prefix + ("raw_rows.csv" if kind == "all" else f"{kind}_rows.csv")
        if kind == "all":
            query = "SELECT * FROM joined"
            view = "joined"
        else:
            changed_dimensions = " OR ".join(
                f"rd{i} IS DISTINCT FROM cd{i}" for i in range(len(selected_dimensions(cfg)))) or "false"
            schema = {r[0]: r[1] for r in con.execute('DESCRIBE main.reference').fetchall()}
            changed_fields = any_field_changed(cfg, schema)
            predicates = {"added": "rp IS NULL", "removed": "cp IS NULL",
                          "changed": "rp AND cp AND rv IS DISTINCT FROM cv",
                          "moved": f"rp AND cp AND ({changed_dimensions})",
                          'field_changed': f'rp AND cp AND ({changed_fields})',
                          "largest_changes": "coalesce(cv,0) IS DISTINCT FROM coalesce(rv,0)"}
            keys = ", ".join(f"k{i}" for i in range(len(cfg["key"])))
            query = f"SELECT *, coalesce(cv,0)-coalesce(rv,0) AS contribution FROM joined WHERE {predicates[kind]}"
            query += f" ORDER BY abs(contribution) DESC, {keys}"
            if "limit" in request:
                query += f" LIMIT {request['limit']}"
            view = f"evidence_{kind}"
            execute(f"CREATE VIEW {view} AS {query}")
        con.execute(f"COPY ({query}) TO {literal(stage / filename)} (HEADER, FORMAT CSV)")
        exports.append({"file": filename, "metric": configured_metrics(cfg)[metric_index]["name"],
                        "kind": kind, "limit": request.get("limit"),
                        "rows": con.execute(f"SELECT count(*) FROM {view}").fetchone()[0],
                        "evidence": ("main" if metric_index == 0 else f"metric_{metric_index}") + "." + view})
    return exports
