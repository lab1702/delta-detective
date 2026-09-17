from jinja2 import Environment, BaseLoader

TEMPLATE = """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Delta detective report</title>
<style>body{font:16px/1.55 system-ui,sans-serif;color:#172b37;max-width:1100px;margin:40px auto;padding:0 24px;background:#fafbfc}h1,h2{line-height:1.2}table{border-collapse:collapse;width:100%;margin:18px 0;background:white}th,td{padding:9px 12px;border-bottom:1px solid #dce3e8;text-align:left;overflow-wrap:anywhere}th{background:#edf2f5}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}small{color:#456}section{margin:34px 0}.badge{padding:8px;background:#e5f2ed}details{margin:15px 0}</style>
<h1>Delta detective</h1><p>Snapshot comparison · {{ cfg.metric.name }} · {{ cfg.metric.aggregate }}</p>
<p class="badge">Reconciliation {{ data.summary.status }} · {{ 'Exact arithmetic' if data.summary.exact else 'Approximate floating-point arithmetic' }}</p>
<section><h2>Comparison and inputs</h2><p>Reference: {{ cfg.reference }}<br>Current: {{ cfg.current }}<br>Declared key columns: {{ cfg.key|join(', ') }}</p>
<p>Raw evidence: {{ 'enabled — raw_rows.csv contains selected keys, before/after metric and dimensions, and side-presence flags for all joined keys.' if cfg.report.include_raw_rows else 'disabled — no raw rows or key values exported.' }}</p></section>
<section><h2>Metric reconciliation</h2><table><tr><th>Measurement</th><th>Value</th></tr>
{% for key in ['reference_total','current_total','delta','added_contribution','removed_contribution','matched_contribution','residual','tolerance'] %}<tr><td>{{ key }}</td><td>{{ data.summary[key] }}</td></tr>{% endfor %}</table>
<p>Relative change: {{ data.summary.percent_change|string + '%' if data.summary.percent_change is not none else 'undefined (reference total is zero)' }}.</p>
<p>Empty populations (sums treated as zero): {{ data.summary.empty_populations|join(', ') or 'none' }}.</p></section>
<section><h2>Keys and selected-field differences</h2><table><tr><th>Measurement</th><th>Rows</th></tr>
{% for k,v in data.summary.items() if k.endswith('_rows') %}<tr><td>{{ k }}</td><td>{{ v }}</td></tr>{% endfor %}</table></section>
<section><h2>Verified contributions</h2>{% for f in data.findings %}<p>{{ f.description }} Rows: {{ f.measurements.rows }}; contribution: {{ f.contribution }}.</p>{% endfor %}</section>
<section><h2>Independent dimension breakdowns</h2><p>Each table reconciles independently. Do not add contributions between tables. Top 50 categories by absolute contribution; any remainder is marked Other.</p>
{% for d in data.dimensions %}<h3>{{ d.name }}</h3><table><tr><th>Category</th><th>Reference rows</th><th>Current rows</th><th>Reference total</th><th>Current total</th><th>Contribution</th></tr>
{% for r in d.rows %}<tr><td>{{ 'Other (remainder)' if r.is_other else 'NULL (missing value)' if r.category is none else 'Value: ' ~ r.category }}</td><td>{{ r.reference_rows }}</td><td>{{ r.current_rows }}</td><td>{{ r.reference_total }}</td><td>{{ r.current_total }}</td><td>{{ r.contribution }}</td></tr>{% endfor %}</table><p>Check: {{ d.check.status }}; residual {{ d.check.residual }}.</p>{% endfor %}</section>
<section><h2>Selected-dimension reclassifications</h2><p>Amounts are before/after totals of moved records, not additional contributions. A record may move in multiple dimensions.</p><table><tr><th>Dimension</th><th>Moved rows</th><th>Also changed metric</th><th>Reference amount</th><th>Current amount</th></tr>{% for r in data.reclassifications %}<tr><td>{{ r.name }}</td><td>{{ r.rows }}</td><td>{{ r.also_metric_changed }}</td><td>{{ r.reference_amount }}</td><td>{{ r.current_amount }}</td></tr>{% endfor %}</table></section>
<section><h2>Validation and schema</h2><ul>{% for check in checks %}<li>{{ check }}</li>{% endfor %}</ul><pre>{{ schemas }}</pre></section>
<details><summary>Methodology and limitations</summary><ul>{% for l in data.limitations %}<li>{{ l }}</li>{% endfor %}</ul><p>Exact integers and decimals are serialized without float conversion; JSON decimal values are strings. Added/current-only values contribute positively, reference-only values negatively, and matched values contribute their difference. Category totals use each side's category membership.</p></details>
<details><summary>Executed SQL and replay queries</summary><pre>{{ sql }}</pre></details></html>"""


def render(cfg, data, checks, sql, schemas):
    return Environment(loader=BaseLoader(), autoescape=True).from_string(TEMPLATE).render(
        cfg=cfg, data=data, checks=checks, sql=sql, schemas=schemas)
