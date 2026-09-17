from decimal import Decimal, localcontext
import math
from .loading import ident
from .config import InvestigationError, selected_dimensions

ABS_TOL = 1e-9
REL_TOL = 1e-12
TOP_GROUPS = 50


def check(reference, current, parts, approximate, absolute_contributions=None):
    with localcontext() as ctx:
        ctx.prec = 100
        delta = current - reference
        residual = delta - sum(parts)
        magnitude = sum(abs(p) for p in parts) if absolute_contributions is None else absolute_contributions
        tolerance = max(ABS_TOL, REL_TOL * max(abs(reference), abs(current), magnitude)) if approximate else 0
        if approximate and not all(math.isfinite(v) for v in [reference, current, *parts, delta, residual, magnitude, tolerance]):
            raise InvestigationError("Floating-point numeric overflow during reconciliation; use smaller values or exact decimal inputs")
        passed = abs(residual) <= tolerance
    if not passed:
        raise InvestigationError(f"Reconciliation failed: residual {residual}, tolerance {tolerance}")
    return {"delta": delta, "residual": residual, "tolerance": tolerance, "status": "passed", "exact": not approximate}


def compare(con, cfg, profiles, execute):
    metric = cfg["metric"]
    is_sum = metric["aggregate"] == "sum"
    typ = profiles["reference"]["schema"][metric["column"]] if is_sum else "BIGINT"
    approximate = typ in ("FLOAT", "DOUBLE")
    if typ.startswith("DECIMAL("):
        scale = int(typ.rstrip(")").split(",")[1])
        cast = f"DECIMAL(38,{scale})"
    else:
        cast = "DOUBLE" if approximate else "HUGEINT"
    val = f"CAST({ident(metric['column'])} AS {cast})" if is_sum else "1::HUGEINT"
    dims = selected_dimensions(cfg)
    key_fields = [f"{ident(k)} AS k{i}" for i, k in enumerate(cfg["key"])]
    dim_fields = [f"{ident(d)} AS d{i}" for i, d in enumerate(dims)]
    fields = cfg.get('compare_fields', [])
    field_columns = [f'{ident(c)} AS f{i}' for i, c in enumerate(fields)]
    for side in ("reference", "current"):
        execute(f"CREATE TABLE {side}_selected AS SELECT {', '.join(key_fields + dim_fields + field_columns)}, {val} AS v, true AS present FROM {side}")
    on = " AND ".join(f"r.k{i}=c.k{i}" for i in range(len(key_fields)))
    columns = ["r.present AS rp", "c.present AS cp", "r.v AS rv", "c.v AS cv"]
    columns += [f"coalesce(r.k{i},c.k{i}) AS k{i}" for i in range(len(key_fields))]
    columns += [x for i in range(len(dims)) for x in (f"r.d{i} AS rd{i}", f"c.d{i} AS cd{i}")]
    columns += [x for i in range(len(fields)) for x in (f'r.f{i} AS rf{i}', f'c.f{i} AS cf{i}')]
    execute(f"CREATE TABLE joined AS SELECT {', '.join(columns)} FROM reference_selected r FULL OUTER JOIN current_selected c ON {on}")
    changed_dims = " OR ".join(f"rd{i} IS DISTINCT FROM cd{i}" for i in range(len(dims))) or "false"
    execute(f"""CREATE TABLE reconciliation AS SELECT
      count(*) FILTER (WHERE rp) AS reference_rows,
      count(*) FILTER (WHERE cp) AS current_rows,
      count(*) FILTER (WHERE rp IS NULL) AS added_rows,
      count(*) FILTER (WHERE cp IS NULL) AS removed_rows,
      count(*) FILTER (WHERE rp AND cp) AS matched_rows,
      count(*) FILTER (WHERE rp AND cp AND rv IS DISTINCT FROM cv) AS metric_changed_rows,
      count(*) FILTER (WHERE rp AND cp AND ({changed_dims})) AS dimension_changed_rows,
      coalesce(sum(rv),0) AS reference_total,
      coalesce(sum(cv),0) AS current_total,
      coalesce(sum(cv) FILTER (WHERE rp IS NULL),0) AS added_contribution,
      -coalesce(sum(rv) FILTER (WHERE cp IS NULL),0) AS removed_contribution,
      coalesce(sum(cv-rv) FILTER (WHERE rp AND cp),0) AS matched_contribution
      FROM joined""")
    cursor = con.execute("SELECT * FROM reconciliation")
    summary = dict(zip([x[0] for x in cursor.description], cursor.fetchone()))
    summary.update(check(summary["reference_total"], summary["current_total"],
                         [summary[x+"_contribution"] for x in ("added", "removed", "matched")], approximate))
    if summary["reference_rows"] != summary["removed_rows"] + summary["matched_rows"] or summary["current_rows"] != summary["added_rows"] + summary["matched_rows"]:
        raise InvestigationError("Key-count identity failed")
    with localcontext() as ctx:
        ctx.prec = 100
        summary["percent_change"] = (Decimal(str(summary["delta"])) / Decimal(str(summary["reference_total"])) * 100) if summary["reference_total"] else None
    summary["empty_populations"] = [s for s in ("reference", "current", "added", "removed", "matched") if summary[s+"_rows"] == 0]
    breakdowns = []
    reclassifications = []
    groups = [[d] for d in cfg["dimensions"]] + cfg.get("dimension_groups", [])
    for i, group in enumerate(groups):
        name = " × ".join(group)
        indices = [dims.index(d) for d in group]
        category = f"d{indices[0]}" if len(group) == 1 else "struct_pack(" + ", ".join(f"{ident(d)} := d{j}" for d, j in zip(group, indices)) + ")"
        moved = " OR ".join(f"rd{j} IS DISTINCT FROM cd{j}" for j in indices)
        table = f"dimension_{i}"
        execute(f"""CREATE TABLE {table} AS WITH r AS (
            SELECT {category} AS category, count(*) AS n, sum(v) AS total FROM reference_selected GROUP BY {category}),
          c AS (SELECT {category} AS category, count(*) AS n, sum(v) AS total FROM current_selected GROUP BY {category})
          SELECT CASE WHEN r.n IS NOT NULL THEN r.category ELSE c.category END AS category,
            coalesce(r.n,0) AS reference_rows, coalesce(c.n,0) AS current_rows,
            coalesce(r.total,0) AS reference_total, coalesce(c.total,0) AS current_total,
            coalesce(c.total,0)-coalesce(r.total,0) AS contribution
          FROM r FULL OUTER JOIN c ON r.category IS NOT DISTINCT FROM c.category""")
        execute(f"CREATE TABLE {table}_ranked AS SELECT *, row_number() OVER (ORDER BY abs(contribution) DESC, category NULLS FIRST) AS rank FROM {table}")
        execute(f"""CREATE TABLE {table}_display AS
          SELECT category, false AS is_other, reference_rows, current_rows, reference_total, current_total, contribution, rank
          FROM {table}_ranked WHERE rank<={TOP_GROUPS}
          UNION ALL SELECT NULL, true, sum(reference_rows)::BIGINT, sum(current_rows)::BIGINT,
            sum(reference_total), sum(current_total), sum(contribution), {TOP_GROUPS+1}
          FROM {table}_ranked WHERE rank>{TOP_GROUPS} HAVING count(*)>0""")
        cur = con.execute(f"SELECT * EXCLUDE(rank) FROM {table}_display ORDER BY rank")
        rows = [dict(zip([c[0] for c in cur.description], row)) for row in cur.fetchall()]
        # Compute the tolerance from all categories, including those folded into Other.
        # Exact arithmetic needs no magnitude sum, which could itself overflow.
        magnitude_sql = "coalesce(sum(abs(contribution)),0)" if approximate else "0"
        total, magnitude = con.execute(f"SELECT coalesce(sum(contribution),0), {magnitude_sql} FROM {table}").fetchone()
        status = check(summary["reference_total"], summary["current_total"], [total], approximate, magnitude)
        check(summary["reference_total"], summary["current_total"], [r["contribution"] for r in rows], approximate, magnitude)
        breakdowns.append({"name": name, "columns": group, "rows": rows, "check": status, "evidence": table + "_display"})
        execute(f"""CREATE TABLE reclassification_{i} AS SELECT count(*) AS rows,
          count(*) FILTER (WHERE rv IS DISTINCT FROM cv) AS also_metric_changed,
          coalesce(sum(rv),0) AS reference_amount, coalesce(sum(cv),0) AS current_amount
          FROM joined WHERE rp AND cp AND ({moved})""")
        r = con.execute(f"SELECT * FROM reclassification_{i}").fetchone()
        reclassifications.append(dict(zip(["rows", "also_metric_changed", "reference_amount", "current_amount"], r), name=name, evidence=f"reclassification_{i}"))
        reclassifications[-1]["movements"] = movement_table(
            con, execute, i, group, indices, moved, reclassifications[-1], approximate)
    return summary, breakdowns, reclassifications


def movement_table(con, execute, index, group, indices, moved, totals, approximate):
    """Bounded transition evidence for matched keys, checked against moved totals."""
    def category(prefix):
        if len(group) == 1:
            return f"{prefix}d{indices[0]}"
        return "struct_pack(" + ", ".join(f"{ident(d)} := {prefix}d{j}" for d, j in zip(group, indices)) + ")"

    table = f"movement_{index}"
    execute(f"""CREATE TABLE {table} AS SELECT
      {category('r')} AS from_category, {category('c')} AS to_category,
      count(*) AS rows, count(*) FILTER (WHERE rv IS DISTINCT FROM cv) AS also_metric_changed,
      sum(rv) AS reference_amount, sum(cv) AS current_amount
      FROM joined WHERE rp AND cp AND ({moved}) GROUP BY 1, 2""")
    execute(f"""CREATE TABLE {table}_ranked AS SELECT *, row_number() OVER (
      ORDER BY rows DESC, from_category NULLS FIRST, to_category NULLS FIRST) AS rank FROM {table}""")
    execute(f"""CREATE TABLE {table}_display AS
      SELECT *, false AS is_other FROM {table}_ranked WHERE rank <= {TOP_GROUPS}
      UNION ALL SELECT NULL, NULL, sum(rows)::BIGINT, sum(also_metric_changed)::BIGINT,
        sum(reference_amount), sum(current_amount), {TOP_GROUPS+1}, true
      FROM {table}_ranked WHERE rank > {TOP_GROUPS} HAVING count(*) > 0""")
    cur = con.execute(f"SELECT * EXCLUDE(rank) FROM {table}_display ORDER BY rank")
    rows = [dict(zip([c[0] for c in cur.description], row)) for row in cur.fetchall()]
    for field in ("rows", "also_metric_changed"):
        if sum(row[field] for row in rows) != totals[field]:
            raise InvestigationError("Movement row-count identity failed")
    checks = {}
    for field in ("reference_amount", "current_amount"):
        magnitude = con.execute(f"SELECT coalesce(sum(abs({field})),0) FROM {table}").fetchone()[0] if approximate else 0
        checks[field] = check(0, totals[field], [row[field] for row in rows], approximate, magnitude)
    return {"columns": group, "rows": rows, "checks": checks, "evidence": table + "_display",
            "ranking": "Moved row count descending, then source and destination categories; top 50 plus Other."}
