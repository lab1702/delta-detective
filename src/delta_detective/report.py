from jinja2 import Environment, BaseLoader

TEMPLATE = """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Delta detective report</title>
<style>body{font:16px/1.55 system-ui,sans-serif;color:#172b37;max-width:1100px;margin:40px auto;padding:0 24px;background:#fafbfc}h1,h2{line-height:1.2}table{border-collapse:collapse;width:100%;margin:18px 0;background:white}th,td{padding:9px 12px;border-bottom:1px solid #dce3e8;text-align:left;overflow-wrap:anywhere}th{background:#edf2f5}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}small{color:#456}section{margin:34px 0}.badge{padding:8px;background:#e5f2ed}details{margin:15px 0}</style>
<h1>Delta detective</h1>
{% set bundle = data %}
<section><h2>Field-level changes: {{ bundle.field_changes.status }}</h2>
{% if bundle.field_changes.fields %}<p>{{ bundle.field_changes.changed_rows }} of {{ bundle.field_changes.matched_rows }} matched records changed in at least one selected field. Added and removed records are excluded. Field counts overlap; do not sum across fields. Blank means exactly empty text; null transitions and blank transitions can overlap. Increased/decreased means later/earlier for dates and times. Raw field values are not shown here.</p>
{% for f in bundle.field_changes.fields %}<h3>{{ f.name }} ({{ f.type }})</h3><table><tr><th>Measurement</th><th>Records</th></tr>{% for key in ['changed_rows','unchanged_rows','became_null','from_null','both_null','value_changed_rows','became_blank','from_blank','increased_rows','decreased_rows'] if key in f %}<tr><td>{{ key }}</td><td>{{ f[key] }}</td></tr>{% endfor %}</table><p>Changed: {{ f.percent_changed|string + '%' if f.percent_changed is not none else 'undefined (no matched records)' }}.</p>{% endfor %}{% endif %}</section>
<section><h2>Schema contract: {{ bundle.schema_checks.status }}</h2>
{% if bundle.schema_checks.status == 'failed' %}<p>Comparison not run: a schema contract was violated. Metric reconciliation, threshold rules, and raw exports were not evaluated.</p>{% endif %}
{% if bundle.schema_checks.results %}<table><tr><th>Input</th><th>Column</th><th>Expected type</th><th>Observed type</th><th>Result</th></tr>{% for s in bundle.schema_checks.results %}<tr><td>{{ s.side }}</td><td>{{ s.column }}</td><td>{{ s.expected_type if s.expected_type is not none else 'Any type for required column; no type specified for extras' }}</td><td>{{ s.observed_type if s.observed_type is not none else 'Missing' }}</td><td>{{ s.status }}{% if s.issue %}: {{ s.issue }}{% endif %}</td></tr>{% endfor %}</table>{% endif %}</section>
{% macro category(value, columns) -%}
{% if columns|length > 1 %}{% for column in columns %}{{ column }}: {{ 'NULL (missing value)' if value[column] is none else 'Value: ' ~ value[column] }}{% if not loop.last %}<br>{% endif %}{% endfor %}{% else %}{{ 'NULL (missing value)' if value is none else 'Value: ' ~ value }}{% endif %}
{%- endmacro %}
<section><h2>Threshold rules: {{ bundle.rule_checks.status }}</h2>
<p>Rules flag changes for review; they do not establish errors or operational causes. Bounds are inclusive. Undefined measurements do not pass. Floating-point metrics retain approximate arithmetic; no reconciliation tolerance is applied to rule bounds.</p>
{% if bundle.rule_checks.results %}<table><tr><th>Rule</th><th>Target / measure</th><th>Observed</th><th>Minimum</th><th>Maximum</th><th>Result</th></tr>
{% for r in bundle.rule_checks.results %}<tr><td>{{ r.name }}</td><td>{{ 'Field: ' ~ r.field if r.field is defined else 'Metric: ' ~ r.metric }} / {{ r.measure }}</td><td>{{ 'See segments below' if r.group_by is defined else r.observed if r.observed is not none else 'undefined' }}{% if r.field is defined %}<br>{{ r.numerator }} of {{ r.denominator }} matched records ({{ r.unit }}){% endif %}</td><td>{{ r.min if 'min' in r else 'none' }}</td><td>{{ r.max if 'max' in r else 'none' }}</td><td>{{ r.status }}{% if r.reason %}: {{ r.reason }}{% endif %}</td></tr>{% endfor %}</table>{% else %}<p>No threshold rules configured.</p>{% endif %}</section>
{% for rule in bundle.rule_checks.results if rule.group_by is defined %}
<section><h3>Segment rule: {{ rule.name }}</h3><p>Grouped by {{ rule.group_by|join(', ') }}. {{ rule.total_segments }} segments evaluated; passed {{ rule.segment_counts.passed }}, failed {{ rule.segment_counts.failed }}, undefined {{ rule.segment_counts.undefined }}. {{ rule.omitted_segments }} omitted from display. Failures and undefined results are shown first, up to 50 segments.</p>
{% if rule.reason %}<p>{{ rule.reason }}</p>{% endif %}
<table><tr><th>Segment</th><th>Observed</th><th>Result</th></tr>{% for s in rule.segments %}<tr><td>{% for k,v in s.segment.items() %}{{ k }}: {{ 'NULL (missing value)' if v is none else 'Value: ' ~ v }}{% if not loop.last %}<br>{% endif %}{% endfor %}</td><td>{{ s.observed if s.observed is not none else 'undefined' }}</td><td>{{ s.status }}{% if s.reason %}: {{ s.reason }}{% endif %}</td></tr>{% endfor %}</table></section>
{% endfor %}
{% for rule in bundle.rule_checks.results if rule.evidence_export is defined %}
<section><h3>Evidence for rule: {{ rule.name }}</h3>{% set e = rule.evidence_export %}
{% if e.status == 'exported' %}<p><a href="{{ e.file }}">{{ e.file }}</a>: {{ e.rows }} of {{ e.total_rows }} eligible records exported. Truncated: {{ 'yes' if e.truncated else 'no' }}. Limit: {{ e.limit }}.</p><p>{{ e.selection }} Rows are ordered by key. A header-only export means no existing records satisfy the selection; a lower-bound failure need not identify offending records.</p>{% else %}<p>Export skipped: {{ e.reason }}</p>{% endif %}</section>
{% endfor %}
{% for data in bundle.metrics %}
<h2>Snapshot comparison &middot; {{ data.metric.name }} &middot; {{ data.metric.aggregate }}</h2>
<p class="badge">Reconciliation {{ data.summary.status }} · {{ 'Exact arithmetic' if data.summary.exact else 'Approximate floating-point arithmetic' }}</p>
<section><h2>Comparison and inputs</h2><p>Reference: {{ cfg.reference }}<br>Current: {{ cfg.current }}<br>Declared key columns: {{ cfg.key|join(', ') }}</p>
<p>Raw evidence: {{ 'enabled; see export inventory below.' if bundle.evidence_exports else 'disabled; no raw rows or key values exported.' }}</p></section>
<section><h2>Metric reconciliation</h2><table><tr><th>Measurement</th><th>Value</th></tr>
{% for key in ['reference_total','current_total','delta','added_contribution','removed_contribution','matched_contribution','residual','tolerance'] %}<tr><td>{{ key }}</td><td>{{ data.summary[key] }}</td></tr>{% endfor %}</table>
<p>Relative change: {{ data.summary.percent_change|string + '%' if data.summary.percent_change is not none else 'undefined (reference total is zero)' }}.</p>
<p>Empty populations (sums treated as zero): {{ data.summary.empty_populations|join(', ') or 'none' }}.</p></section>
<section><h2>Keys and selected-field differences</h2><table><tr><th>Measurement</th><th>Rows</th></tr>
{% for k,v in data.summary.items() if k.endswith('_rows') %}<tr><td>{{ k }}</td><td>{{ v }}</td></tr>{% endfor %}</table></section>
<section><h2>Verified contributions</h2>{% for f in data.findings %}<p>{{ f.description }} Rows: {{ f.measurements.rows }}; contribution: {{ f.contribution }}.</p>{% endfor %}</section>
<section><h2>Independent dimension breakdowns</h2><p>Each table reconciles independently. Do not add contributions between tables. Top 50 categories by absolute contribution; any remainder is marked Other.</p>
{% for d in data.dimensions %}<h3>{{ d.name }}</h3><table><tr><th>Category</th><th>Reference rows</th><th>Current rows</th><th>Reference total</th><th>Current total</th><th>Contribution</th></tr>
{% for r in d.rows %}<tr><td>{% if r.is_other %}Other (remainder){% elif d.columns|length > 1 %}{% for column in d.columns %}{{ column }}: {{ 'NULL (missing value)' if r.category[column] is none else 'Value: ' ~ r.category[column] }}{% if not loop.last %}<br>{% endif %}{% endfor %}{% else %}{{ 'NULL (missing value)' if r.category is none else 'Value: ' ~ r.category }}{% endif %}</td><td>{{ r.reference_rows }}</td><td>{{ r.current_rows }}</td><td>{{ r.reference_total }}</td><td>{{ r.current_total }}</td><td>{{ r.contribution }}</td></tr>{% endfor %}</table><p>Check: {{ d.check.status }}; residual {{ d.check.residual }}.</p>{% endfor %}</section>
<section><h2>Selected-dimension reclassifications</h2><p>Amounts are before/after totals of moved records, not additional contributions. A record may move in multiple dimensions.</p><table><tr><th>Dimension</th><th>Moved rows</th><th>Also changed metric</th><th>Reference amount</th><th>Current amount</th></tr>{% for r in data.reclassifications %}<tr><td>{{ r.name }}</td><td>{{ r.rows }}</td><td>{{ r.also_metric_changed }}</td><td>{{ r.reference_amount }}</td><td>{{ r.current_amount }}</td></tr>{% endfor %}</table></section>
<section><h2>Category movements</h2><p>Matched keys that changed category, ranked by moved row count. Top 50 transitions plus Other. Before/after amounts describe moved records; they are not additional contributions to the overall change. Tables can overlap across dimensions.</p>
{% for r in data.reclassifications %}{% set m = r.movements %}<h3>{{ r.name }}</h3>
{% if m.rows %}<table><tr><th>From</th><th>To</th><th>Moved rows</th><th>Also changed metric</th><th>Amount before</th><th>Amount after</th></tr>
{% for row in m.rows %}<tr><td>{{ 'Other (remainder)' if row.is_other else category(row.from_category, m.columns) }}</td><td>{{ 'Other (remainder)' if row.is_other else category(row.to_category, m.columns) }}</td><td>{{ row.rows }}</td><td>{{ row.also_metric_changed }}</td><td>{{ row.reference_amount }}</td><td>{{ row.current_amount }}</td></tr>{% endfor %}</table>
<p>Before/after amount checks: {{ m.checks.reference_amount.status }} / {{ m.checks.current_amount.status }}.</p>{% else %}<p>No matched keys changed category.</p>{% endif %}
{% else %}<p>No dimensions configured.</p>{% endfor %}</section>
{% endfor %}
<section><h2>Evidence exports</h2>{% if bundle.evidence_exports %}<p>These files contain raw keys and selected fields. Changed exports include matched metric changes only; largest changes include nonzero additions, removals, and matched metric changes.</p><table><tr><th>File</th><th>Metric</th><th>Selection</th><th>Rows</th><th>Limit</th></tr>{% for e in bundle.evidence_exports %}<tr><td>{{ e.file }}</td><td>{{ e.metric }}</td><td>{{ e.kind }}</td><td>{{ e.rows }}</td><td>{{ e.limit if e.limit is not none else 'All qualifying rows' }}</td></tr>{% endfor %}</table>{% else %}<p>No raw evidence exported.</p>{% endif %}</section>
<section><h2>Validation and schema</h2><ul>{% for check in checks %}<li>{{ check }}</li>{% endfor %}</ul><pre>{{ schemas }}</pre></section>
<details><summary>Methodology and limitations</summary><ul>{% for l in data.limitations %}<li>{{ l }}</li>{% endfor %}</ul><p>Exact integers and decimals are serialized without float conversion; JSON decimal values are strings. Added/current-only values contribute positively, reference-only values negatively, and matched values contribute their difference. Category totals use each side's category membership.</p></details>
<details><summary>Executed SQL and replay queries</summary><pre>{{ sql }}</pre></details></html>"""


def render(cfg, data, checks, sql, schemas):
    return Environment(loader=BaseLoader(), autoescape=True).from_string(TEMPLATE).render(
        cfg=cfg, data=data, checks=checks, sql=sql, schemas=schemas)
