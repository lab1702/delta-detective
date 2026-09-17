"""Evaluate user policy independently of arithmetic/data validation."""
from decimal import Decimal, localcontext

from .config import threshold_number


def evaluate_rules(rules, results):
    metrics = {item["metric"]["name"]: item for item in results}
    evaluated = []
    for rule in rules:
        item = metrics[rule["metric"]]
        summary = item["summary"]
        measure = rule["measure"]
        reason = None
        # Match the precision used by reconciliation and percentage reporting.
        with localcontext() as ctx:
            ctx.prec = 100
            if measure == "removed_percent":
                value = (Decimal(summary["removed_rows"]) * 100 / summary["reference_rows"]
                         if summary["reference_rows"] else None)
                if value is None:
                    reason = "Undefined: reference row count is zero."
            elif measure in ("abs_delta", "abs_percent_change"):
                value = summary[measure[4:]]
                value = abs(value) if value is not None else None
            else:
                value = summary[measure]
            if value is None and reason is None:
                reason = "Undefined: reference metric total is zero."
            bounds = {k: threshold_number(rule[k]) for k in ("min", "max") if k in rule}
            observed = Decimal(str(value)) if value is not None else None
            status = "undefined" if observed is None else "passed"
            if observed is not None and (("min" in bounds and observed < bounds["min"])
                                         or ("max" in bounds and observed > bounds["max"])):
                status = "failed"
                reason = "Observed value is outside the inclusive bounds."
        evaluated.append({"name": rule["name"], "metric": rule["metric"], "measure": measure,
                          "observed": value, **bounds, "status": status, "reason": reason,
                          "exact": summary["exact"] if measure not in ("removed_percent", "current_rows", "added_rows", "removed_rows") else True,
                          "evidence": f"analysis.sql: {item['sql_schema']}.reconciliation"})
    return {"status": ("not_configured" if not evaluated else
                       "failed" if any(r["status"] != "passed" for r in evaluated) else "passed"),
            "results": evaluated}
