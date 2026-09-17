import json
from decimal import Decimal
from .config import InvestigationError


LIMITATIONS = [
    "These are versions of the same logical dataset, not reporting periods.",
    "Verified differences and arithmetic contributions do not establish operational causes, failures, or real-world changes.",
    "Dimensions are alternative views of the same change; never add contributions across dimensions.",
    "Only selected metrics, dimensions, and compare_fields are compared after matching keys. Other row fields are not checked for changes.",
    "Aggregate-only reports are not anonymous: categories and totals may be sensitive.",
    "Rerunning analysis.sql requires matching input files at the recorded paths and a compatible DuckDB version.",
    "Selected column types must match exactly. CSV inference can interpret numeric-looking identifiers; use explicit CSV types or typed Parquet when identity or decimal precision matters.",
    "Floating-point checks use max(1e-9, 1e-12 * max(abs(reference), abs(current), sum(abs(contributions)))).",
]


def dumps(value):
    def encode(obj):
        if isinstance(obj, Decimal):
            return str(obj)
        raise TypeError(f"Cannot serialize {type(obj)}")
    try:
        return json.dumps(value, default=encode, ensure_ascii=False, indent=2, allow_nan=False)
    except ValueError as exc:
        raise InvestigationError("Cannot serialize analysis: nonfinite result or numeric overflow") from exc


def build_findings(summary, breakdowns, reclassifications, schema_changes):
    findings = []
    for kind, description in [("added", "Keys present only in the current snapshot."),
                              ("removed", "Keys present only in the reference snapshot."),
                              ("matched", "Keys present in both snapshots; contribution is current minus reference metric.")]:
        findings.append({"id": "population." + kind, "type": "arithmetic_contribution", "description": description,
                         "measurements": {"rows": summary[kind+"_rows"]}, "contribution": summary[kind+"_contribution"],
                         "evidence": "analysis.sql: reconciliation", "method": "Full outer join on validated unique non-null key columns",
                         "exact": summary["exact"], "limitations": ["No operational cause is established."]})
    return {"summary": summary, "findings": findings, "dimensions": breakdowns,
            "reclassifications": reclassifications, "schema_changes": schema_changes, "limitations": LIMITATIONS}
