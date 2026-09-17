from pathlib import Path
import shutil
import tempfile
from datetime import datetime, timezone
import duckdb
from .config import load_config, InvestigationError
from .loading import load_and_validate, fingerprint, literal
from .comparison import compare
from .findings import build_findings, dumps, LIMITATIONS
from .report import render


def prepare_output(out, overwrite, inputs=()):
    out = Path(out).resolve()
    for source in inputs:
        source = Path(source).resolve()
        if source == out or out in source.parents:
            raise InvestigationError("Output directory must not contain configuration or input files")
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
        profiles = load_and_validate(con, cfg, execute)
        summary, dimensions, reclassifications = compare(con, cfg, profiles, execute)
        for s in profiles:
            if before[s] != profiles[s]["sha256"] or fingerprint(cfg[s]) != before[s]:
                raise InvestigationError("Input changed during analysis; rerun with stable snapshots")
        a, b = (profiles[s]["schema"] for s in ("reference", "current"))
        schema_changes = [{"column": c, "reference_type": a.get(c), "current_type": b.get(c)} for c in sorted(a.keys() | b.keys()) if a.get(c) != b.get(c)]
        data = build_findings(summary, dimensions, reclassifications, schema_changes)
        checks = ["Passed: required files/columns, null and duplicate keys, selected type compatibility, key-count identities, overall and independent dimension reconciliation.",
                  "Passed: numeric, finite, non-null sum values." if cfg["metric"]["aggregate"] == "sum" else "Skipped: numeric metric validation (COUNT(*) selected).",
                  "Ran: schema comparison (see below). Unrelated schema changes are informational.",
                  "Skipped: comparisons of nonselected row attributes; operational-cause investigation."]
        data["validation"] = checks
        replay = ["SELECT * FROM reconciliation;"] + [f"SELECT * FROM dimension_{i}_display ORDER BY rank;\nSELECT * FROM reclassification_{i};" for i in range(len(dimensions))]
        sql = "-- Run in a fresh DuckDB database; matching input files are required.\n" + "\n\n".join(statements + replay)
        if cfg["report"]["include_raw_rows"]:
            con.execute(f"COPY joined TO {literal(stage / 'raw_rows.csv')} (HEADER, FORMAT CSV)")
        manifest = {"application_version": "0.1.0", "duckdb_version": duckdb.__version__,
                    "created_utc": datetime.now(timezone.utc).isoformat(), "configuration": cfg,
                    "inputs": profiles, "execution_status": "success", "reconciliation": {k: summary[k] for k in ("status", "exact", "residual", "tolerance")},
                    "validation": checks, "assumptions_and_limitations": LIMITATIONS,
                    "raw_evidence": {"included": cfg["report"]["include_raw_rows"],
                        "contents": "All joined keys: kN key columns in configured order; rv/cv metric values; rdN/cdN dimensions in configured order; rp/cp side presence. Only selected fields, not full source rows."}}
        for filename, content in [("findings.json", dumps(data)), ("manifest.json", dumps(manifest)),
                                  ("analysis.sql", sql), ("report.html", render(cfg, data, checks, sql, dumps({"changes": schema_changes, "inputs": profiles})))]:
            (stage / filename).write_text(content, encoding="utf-8")
        if out.exists():
            # The resolved output has been checked not to contain any source inputs.
            shutil.rmtree(out)
        stage.replace(out)
        return data
    except duckdb.Error as exc:
        # Do not echo DuckDB's offending row/key values in default diagnostics.
        message = str(exc)
        for reason in ("duplicate keys", "null key component", "null or nonfinite metric"):
            if reason in message:
                side = "reference" if "reference:" in message else "current"
                raise InvestigationError(f"{side}: {reason}; repair the source snapshot and rerun") from None
        category = "numeric overflow" if "overflow" in message.lower() or "out of range" in message.lower() else "parse, type, or SQL execution error"
        raise InvestigationError(f"DuckDB {category}; verify CSV structure and inferred types or supply explicitly typed Parquet. No successful bundle was written.") from None
    finally:
        con.close()
        if stage.exists():
            shutil.rmtree(stage)
