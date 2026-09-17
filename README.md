# delta-detective

Two datasets in. A reconciled difference and an inspectable trail of evidence out.

A local Python CLI for comparing **two versions of the same logical dataset**.
DuckDB performs loading, validation, joins, and aggregation. Python receives only
bounded aggregate summaries (at most 51 rows per breakdown per metric), never raw
datasets. Segment rules scan all segment aggregates in batches of 50 and retain
at most 50 displayed results per rule.
No service, network calls, telemetry, pandas, or generated explanations are used.

## Install and run

Requires Python 3.11 or newer.

```sh
python -m venv .venv
# Windows PowerShell:
.venv\Scripts\Activate.ps1
# macOS/Linux: source .venv/bin/activate
python -m pip install -e ".[test]"
delta-detective demo --out ./demo-data
delta-detective investigate ./demo-data/comparison.yaml --out ./investigation
python -m pytest -q
```

Open `investigation/report.html` directly in your browser. It contains all styling
and analysis details, with no external resources. An investigation summary at the
top distinguishes blocked comparisons, completed runs with rules needing review,
completed runs with all rules passed, and completed runs with no configured rules.
It shows verified totals/deltas, key populations, failed or undefined rules with
their bounds, and selected category/field changes. Internal links lead to details;
enabled rule evidence exports have local download links and truncation counts.

The summary is deterministic and available as `investigation_summary` in both
findings and manifest JSON. It shows all metrics in configuration order (never
ranks unlike metric units), up to ten non-passing rules, three largest nonzero
category contributions per metric excluding Other, and five fields ranked by
changed-record count. Ties retain report order. Dimension views can overlap;
contributions are not added across them. The summary states observations and rule
outcomes without inferred causes. Schema-blocked runs show contract violations
and never claim that comparison or threshold evaluation completed.

Output directories must be empty
or absent; use `--overwrite` to explicitly replace an existing bundle. An output
directory cannot contain an input or the configuration. An execution failure
leaves a prior bundle untouched and exits nonzero. Finding differences is success
(exit 0), not an execution error. CLI input/execution errors exit 2. With
`--fail-on-rule-violation`, failed or undefined threshold rules exit 3 after the
completed bundle is written.
Schema contract violations instead write a schema-only bundle and exit 4; replacing
an existing bundle still requires `--overwrite`.

In a restricted workspace where the OS temporary directory is inaccessible, run
`python -m pytest -q --basetemp .test-tmp-local` with a fresh dedicated test directory.
Pytest owns and may remove that directory.

## Configuration

To create a configuration interactively:

```sh
delta-detective init reference.parquet current.parquet
# Or choose a new output file:
delta-detective init reference.csv current.csv --out comparisons/orders.yaml
```

The wizard first asks **Configure input parsing, column mappings, or filters?**
Enter skips these optional steps. Answer yes to:

- Set CSV parsing options separately for each CSV input **before loading**. Choose
  an option number, then enter a source column name and DuckDB type, a header
  boolean (`true`/`false`), or a JSON string for delimiter, quote, escape, null
  marker, and date/time formats. For example, enter `";"`, `""`, or `"\\t"`.
  Unspecified settings retain automatic inference and existing defaults.
- Select columns to rename in each loaded snapshot and enter their logical names.
  Mappings are simultaneous; collisions prompt you to re-enter that side's
  mapping. Later menus use the mapped names.
- Add shared filters using numbered column/operator menus and JSON values. Use
  quoted ISO dates and exact decimals; membership takes a JSON list. Null checks
  need no value. Each added condition previews the combined AND filter's included,
  excluded, and total counts for both inputs, without showing source row values.

The chosen filters determine the rows used for key candidates, available sum
metrics, field comparisons, and validation during setup. An empty subset is
reported explicitly. Enter finishes each optional menu; EOF or Ctrl+C cancels
without writing a configuration. Parsing errors abort safely without saving.

The wizard then displays column names and types from both snapshots and offers
numbered menus for key columns, count/sum metrics, dimensions, and combined
dimension groups. Enter comma-separated numbers for multiple selections. Choose
metric names and optionally add threshold rules with inclusive minimum/maximum
bounds. Blank optional selections skip that step; EOF or
Ctrl+C cancels without writing a configuration.

Answer yes to **Configure schema, field comparisons, scoped rules, or exports?**
to enable the expanded options. Enter skips them and keeps the basic setup flow.
The expanded menus let you:

- Select required schema columns, accept or override their inferred types, or use
  `*` for presence only; add additional required column names and optionally reject
  extra columns. Inferred types are suggestions, not a guarantee of correctness.
- Choose supported non-key comparison fields. Nonfinite floating-point fields
  are excluded, while null-valued fields remain available.
- Optionally set absolute tolerances for selected numeric comparison fields.
  Enter skips tolerances and preserves exact comparison. Invalid or negative
  bounds prompt for another value; decimal text is preserved exactly.
- Enable full or focused raw exports and set positive row limits for focused
  selections. Enter leaves all exports disabled. Full exports include all rows.
- Choose an overall metric, a configured dimension/group, or a compared field as
  a rule target. Field measure menus omit incompatible blank/direction measures.
- Optionally restrict a segment rule using typed JSON scalar selectors, such as
  `"West"`, `12`, `true`, or `null`. Source category values are never enumerated.
- Enable supporting-record exports for individual failed rules, with a limit
  defaulting to 100. Export choices are explicit and default to no.

Candidate single-column keys are checked for uniqueness and nulls in both selected
snapshots. You must select the logical key yourself: uniqueness does not establish
record identity. You can select a composite key even when its individual columns
are not unique; the selected tuple is checked before continuing. Empty snapshots
provide no evidence for identity. Only columns with matching supported types are
selectable, and sum choices exclude null/nonfinite columns. No raw row values are
displayed. CSV inference has the same limitations described below, including
numeric-looking identifiers; use CSV type overrides or typed Parquet to preserve those identifiers.

The wizard uses the investigation loader and runs validation and reconciliation
for every selected metric and breakdown before saving when the schema contract
passes or is absent. If a declared contract conflicts with the current inputs,
the wizard warns and saves it for a schema-only investigation (exit 4), skipping
metric validation just as `investigate` does. This scans full inputs
and can take time on large datasets; it is not a schema-only preview. It writes
one UTF-8 YAML file (default `comparison.yaml`) with absolute input paths and raw
exports disabled unless explicitly selected. Existing files are never overwritten. No report bundle is
created until you run `investigate`. Threshold rules are validated as configuration;
their pass/fail results are reported during the investigation.

You can also write the YAML directly:

```yaml
mode: snapshots
reference: reference.parquet
current: current.parquet
key: [order_id]               # composite keys: [order_id, line_id]
metric:
  name: net_sales
  aggregate: sum
  column: net_amount
  null_policy: error
dimensions: [region, source]
report:
  include_raw_rows: false
```

Paths resolve relative to this YAML file. Only local `.csv` and `.parquet` inputs
are supported. No URLs or SQL configuration.
Input paths, including parent directories, cannot contain glob characters
(`*`, `?`, `[` or `]`), so DuckDB reads exactly the file recorded in the manifest.
Hive partition inference is disabled for both formats: parent directory names
never supply or replace column values.
YAML uses a safe loader; duplicate mapping keys, unknown fields, and contradictory
options are rejected.
For **COUNT(*)**, replace the metric with `{name: rows, aggregate: count}`;
neither `column` nor `null_policy` is allowed. `dimensions` defaults to `[]` and
raw evidence defaults to false. Keys cannot also be metrics or dimensions,
to avoid exposing key values in aggregate reports.

### Column-name mapping

Use optional per-input mappings when the same logical columns have different
names in the source files. Each entry maps an exact source name to the name used
throughout the comparison:

```yaml
column_mapping:
  current:
    customerId: customer_id
    netAmount: net_amount
```

Both `reference` and `current` accept mappings, for CSV or Parquet. Unlisted
columns keep their names. Mappings apply simultaneously, so swaps are supported;
source names refer to the original loaded schema, not the result of another
mapping. Source names must match exactly, including case. Missing source columns,
empty names, and target collisions (including collisions with unrenamed columns)
fail the run. Collision checks follow DuckDB's ASCII case-insensitive identifier
rules; automatic suffixes are never used to resolve mapped-name collisions.

CSV parsing and `csv.types` use **source names**. Schema contracts, keys, metrics,
dimensions, field comparisons, rules, and evidence exports use **mapped names**.
Mapping changes only names, preserving all values, types, rows, and column order.
It happens before schema validation; incompatible types still fail validation.

The manifest records each input's `source_schema`, `column_mapping`, and mapped
`schema`, and replay SQL includes the explicit column aliases. Inputs are never
modified. Each run is standalone and DuckDB resource settings remain unchanged.
Add mappings directly to YAML or use the init wizard's optional input setup.
The wizard inspects source names before mapping, then offers logical names in
the remaining menus.

### Filtered comparisons

Use a shared `filters` list to compare a subset of both snapshots. All conditions
must match (AND); omitted or empty filters compare the full snapshots.

```yaml
filters:
  - column: currency
    operator: equals
    value: USD
  - column: region
    operator: in
    value: [East, West]
  - column: order_date
    operator: gte
    value: "2026-01-01"
  - column: amount
    operator: lt
    value: "1000.00"
```

Supported operators are `equals`, `in` (a nonempty list in `value`), `gt`, `gte`,
`lt`, `lte`, `is_null`, and `is_not_null`. Null checks must omit `value`. Null
values fail other conditions, including membership; null entries in membership
lists are rejected. Bounds apply to numeric or DATE columns. Equality and
membership support numeric, VARCHAR, BOOLEAN, and DATE columns. Text values must
be strings and booleans must be YAML booleans. DATE values must be quoted ISO
`YYYY-MM-DD` strings; timestamp filters are not supported. Numeric values must be
finite and fit a decimal precision of 38; quote decimal bounds to avoid YAML
floating-point precision loss. Bounds are not rounded to the column's scale.
Floating-point source columns retain approximate comparison semantics.

Filters use mapped column names and require those columns to exist with matching
types in both inputs. The complete files are parsed and schema contracts checked
first. Filters then select rows before key/value validation, joins, metrics,
field comparisons, threshold rules, and exports. Excluded rows are not checked
for duplicate/null keys or metric validity, but malformed input files still fail.
No source file is modified, and DuckDB resource defaults remain unchanged.

**Entering or leaving the selected subset appears as an addition or removal**,
even when the record's key exists in both original files. Every reported metric
and threshold result describes the selected population. The report prominently
shows this scope and each input's total, included, and excluded row counts.

`filter_scope` in findings and manifest JSON records the filters, status, counts,
and scope explanation. Replay SQL applies the same predicates and retains count
tables named `reference_filter_scope` and `current_filter_scope`. Schema-blocked
runs mark filters as `not_evaluated`. Each run is standalone. Add filters directly
to YAML or use the init wizard's optional input setup to preview population counts.

### Schema contracts

An optional `schema` section defines the expected structure of **each** snapshot,
independently of whether the two observed schemas match:

```yaml
schema:
  columns:
    order_id: VARCHAR
    net_amount: DECIMAL(18,2)
    region: null
  allow_extra_columns: true
```

Every listed column is required. A type string requires that exact DuckDB type;
`null` requires presence but accepts any type. Type aliases and formatting are
normalized (`int` becomes `INTEGER`, `text` becomes `VARCHAR`). Decimal precision
and scale must match. Column names are matched exactly as recorded in the loaded
schema. Set `allow_extra_columns: false` to reject every column not listed;
the default is true. The columns mapping must be nonempty, and unknown contract
fields or invalid type strings are configuration errors.

Contracts inspect loaded types; they do not cast values, override CSV parsing,
or repair sources. For example, if both files infer a numeric order ID but the
contract requires `VARCHAR`, both fail even though they match each other.
Typed Parquet is preferable when identifier formatting or exact decimals matter.

Results appear in the CLI, HTML, and `schema_checks` in both findings and manifest
JSON. Each row records the input side, column, expected and observed types, status,
and any `missing_column`, `type_mismatch`, or `unexpected_column` issue. Without a
contract the status is `not_configured`; otherwise it is `passed` or `failed`.

A violation stops the comparison before key validation, metric reconciliation,
threshold evaluation, or raw exports. The run writes a schema-only evidence
bundle and the CLI exits **4**, regardless of `--fail-on-rule-violation`. Both JSON
files have `execution_status: schema_contract_failed`; findings have `summary:
null`, `metrics: []`, and threshold status `not_evaluated`. The Python API returns
this result instead of raising for a contract violation. SQL replay loads the
inputs and describes their schemas; contract evaluation remains in Python.

Malformed configuration and unreadable inputs remain execution errors (exit 2)
and leave prior output untouched. A schema-only bundle can replace a previous
bundle only with `--overwrite`, through the same staged publication as a successful
comparison. Passing a contract does not bypass the existing duplicate/null key,
selected-type compatibility, metric, or reconciliation checks.

Configure contracts through the expanded `init` options or directly in YAML.
Contracts need no previous run or stored baseline.

### Multiple metrics, combined dimensions, and focused exports

Use `metrics` instead of `metric` to compare several measurements in one bundle.
Existing single-metric configurations continue to work. This example can also
be used with the generated demo inputs:

```yaml
mode: snapshots
reference: reference.parquet
current: current.parquet
key: [order_id]
metrics:
  - name: net_sales
    aggregate: sum
    column: net_amount
    null_policy: error
  - name: orders
    aggregate: count
dimensions: [region, source]
dimension_groups:
  - [region, source]
report:
  evidence_exports:
    - kind: removed
    - kind: largest_changes
      limit: 25
```

Metric names must be unique; specify exactly one of `metric` or `metrics`.
Every metric has its own arithmetic checks, contribution tables, and HTML/CLI
summary. All selected metrics must validate for the bundle to succeed. Inputs
are loaded once; joins and aggregations run separately per metric, so additional
metrics increase memory and execution costs.

`dimension_groups` contains lists of at least two distinct column names. Group
columns need not also appear in `dimensions`. Each group reconciles independently
and retains the top 50 combinations plus an explicit Other remainder. Combined
categories are structured objects in JSON, preserving component types and nulls.
Reclassification counts for a group count each matched key once if any component
changed. Do not add contributions across breakdowns or across different metrics.

`evidence_exports` explicitly opts into exporting raw keys and selected fields,
even when `include_raw_rows` is false. Each requested selection is exported for
each metric:

| Kind | Selection |
| --- | --- |
| `added` | Keys present only in the current snapshot |
| `removed` | Keys present only in the reference snapshot |
| `changed` | Matched keys whose metric value changed; excludes category-only moves |
| `moved` | Matched keys with any selected dimension or group column changed, including category-only moves |
| `field_changed` | Matched keys with any configured `compare_fields` value changed, including zero metric contribution |
| `largest_changes` | Nonzero row contributions across additions, removals, and matches |

Focused exports include a signed `contribution` column. They sort by absolute
contribution descending, with configured key columns as deterministic tie breakers.
Each kind accepts an optional positive integer `limit`; `largest_changes` defaults
to 100, while other kinds default to all qualifying rows. Empty selections still
produce a CSV header. Count metrics have no matched metric changes.

With one metric, files are named `removed_rows.csv`, `largest_changes_rows.csv`,
and so on. With multiple metrics, each filename starts with its zero-based metric
index, such as `metric_0_removed_rows.csv`. `include_raw_rows: true` independently
adds the full `raw_rows.csv` export (with the same prefix rule). The report and
manifest list every exported file, metric, selection, limit, and row count.

### Field-level change summaries

Use `compare_fields` to track non-key attributes without treating them as metrics
or grouping dimensions:

```yaml
compare_fields: [status, delivery_date, description]
report:
  evidence_exports:
    - kind: field_changed
      limit: 100
```

The export is optional. With only `compare_fields`, the report contains aggregate
counts, never before/after field values. Fields independently selected as
dimensions still have their usual category breakdowns.

Comparisons cover matched keys only; added and removed records are excluded.
Each field reports changed and unchanged counts, non-null-to-null (`became_null`),
null-to-non-null (`from_null`), both-null, and non-null value changes. Text fields
also count transitions to and from exactly empty text. Whitespace-only text is
not blank. Null-to-empty counts as both `from_null` and `became_blank`; these
measures can overlap. CSV empty fields are read as null by default; use a
different `nullstr` marker with a `VARCHAR` type override, or typed Parquet,
to preserve a distinct empty-string value.

Numeric fields report increases/decreases; date/time fields use the same counters
for later/earlier values. Direction counts exclude null transitions. Text and
boolean fields have no direction counters. Equality is null-safe and compares
stored values without trimming or case folding. Numeric comparisons are exact
unless an absolute field tolerance is configured, including for FLOAT/DOUBLE.
Nonfinite floating-point comparison fields are rejected; nulls are
allowed. Fields must exist with exactly matching types in both snapshots.
Supported types are text, boolean, numeric, date, and time/timestamp types;
nested values and other types are rejected. Keys cannot be compared fields.

`field_changes` in findings and manifest JSON contains a matched-record overview
and a `fields` list in configuration order. `changed_rows` in the overview counts
each matched key once even if multiple fields changed. Per-field change percentages
use all matched keys as the denominator and are undefined when there are none.
Counts across fields overlap. The status `passed` means the counts were computed
and checked, not that there were no changes. No fields yields `not_configured`;
a schema-contract failure yields `not_evaluated`.

This analysis runs once for the investigation, independently of the number of
metrics. Replay SQL provides `main.field_overview` and `main.field_N`, where N is
the zero-based field index. Raw `rfN`/`cfN` columns in the joined tables preserve
the before/after values in `compare_fields` order.

The opt-in `field_changed` export uses the existing focused-export conventions:
one row per changed matched key per metric, sorted by absolute metric contribution
then key, with an optional positive limit. Files are `field_changed_rows.csv` or
`metric_0_field_changed_rows.csv`, etc. Configuring this export requires at least
one compared field. All raw exports, including `include_raw_rows`, include the
selected `rfN`/`cfN` columns when fields are configured. The manifest records their
column order. CSV null/empty rendering has the same ambiguity as other raw exports;
SQL replay retains typed evidence. Field changes do not trigger threshold rules
or change exit codes by themselves; use field rules below to enforce limits.

Select comparison fields through the expanded `init` options or directly in YAML.
Only the current run's two snapshots are needed.

### Numeric field tolerances

Use `field_tolerances` to ignore small numeric differences in selected comparison
fields while preserving the original values and metric totals:

```yaml
compare_fields: [unit_price, status]
field_tolerances:
  unit_price:
    absolute: "0.01"
```

Each tolerance must name a numeric `compare_fields` column using its mapped name.
Only an `absolute` bound is supported. It must be finite, nonnegative, and fit
decimal precision 38. Quote decimal values to avoid YAML floating-point rounding.
Omitting a tolerance keeps exact comparison; zero also keeps exact comparison.

For two non-null values, a difference **at or below** the absolute bound counts
as unchanged. Per-field results split `unchanged_rows` into `exact_match_rows`
(including both-null pairs) and `within_tolerance_rows` (distinct, non-null values
within the bound). Null-to-value and value-to-null transitions always count as
changes. `value_changed_rows`, increases/decreases, the overall changed-record
count, and field change percentages use the same tolerance-aware definition.

Existing field threshold measures and their supporting-record exports follow
these definitions, as does the general `field_changed` export. An unchanged-row
rule includes both exact matches and differences within tolerance. The new exact
and within-tolerance counters are informational, not additional rule measures.

Metric totals, reconciliation, metric-based rules, `changed`/`largest_changes`
exports, and dimension movements remain unaffected, even if the same column is
also a metric or dimension. Before/after values in raw evidence are never rounded
or replaced. Integer/decimal tolerance comparisons use exact arithmetic, including
wide values whose subtraction would overflow the source type; FLOAT/DOUBLE
comparisons retain approximate floating-point semantics.

The report displays each configured bound and both unchanged counters. Findings
and manifest JSON include `absolute_tolerance` per field (null for exact default),
and the manifest configuration and replay SQL retain the chosen policy. The
expanded wizard offers tolerance setup after numeric comparison fields are
selected. Each run remains standalone and DuckDB resource defaults are unchanged.

### Field-level threshold rules

Rules can target a compared field using `field` instead of `metric`:

```yaml
compare_fields: [status, delivery_date, description]
rules:
  - name: At most two percent of statuses change
    field: status
    measure: percent_changed
    max: 2
  - name: Delivery dates must not become null
    field: delivery_date
    measure: became_null
    max: 0
  - name: Descriptions must not become blank
    field: description
    measure: became_blank
    max: 0
  - name: At most ten delivery dates move later
    field: delivery_date
    measure: increased_rows
    max: 10
```

A rule must specify exactly one of `field` or `metric`. Field targets must be in
`compare_fields`. They apply to all matched keys, once per investigation regardless
of the number of metrics; `group_by` and `where` are not supported on field rules.

Count measures are `changed_rows`, `unchanged_rows`, `became_null`, `from_null`,
`both_null`, `value_changed_rows`, `became_blank`, `from_blank`, `increased_rows`,
and `decreased_rows`. Each has a percentage form: prefix with `percent_` and omit
the `_rows` suffix, for example `percent_changed`, `percent_became_null`,
`percent_value_changed`, and `percent_increased`. Blank measures require text;
direction measures require numeric or date/time fields. Invalid combinations
are rejected rather than silently treated as zero.

All percentages use **all matched records** as their denominator, including
unchanged and null-valued records, and exclude added/removed records. Percentage
bounds use 2 for 2%, with the same 100-digit decimal calculation precision as other
reported percentages. An empty matched population yields zero for count rules
and undefined for percentage rules. Bounds are inclusive; null/blank overlap and
stored-value equality follow the field comparison semantics above.

Field rule results in `rule_checks` include `field`, `observed`, `numerator`,
`denominator`, `denominator_basis`, `unit`, bounds, status, and evidence pointers
to `main.field_N` and `main.field_overview`. HTML and CLI also show the count and
matched-record denominator. Failed or undefined field rules use the existing
overall failure status and opt-in `--fail-on-rule-violation` exit code 3 after the
bundle is saved. Raw field values remain excluded unless exports are enabled.
The expanded `init` options can author field rules and their evidence exports.

### Rule-specific evidence exports

Add `export` to an individual threshold rule to save supporting records when the
rule fails or is undefined. This explicitly opts into exporting raw keys and
selected before/after values, even when `include_raw_rows` is false:

```yaml
compare_fields: [delivery_date]
rules:
  - name: Delivery dates must not become null
    field: delivery_date
    measure: became_null
    max: 0
    export: {limit: 100}
  - name: Each region retains its records
    metric: orders
    group_by: [region]
    measure: removed_percent
    max: 5
    export: {limit: 200}
```

Use the existing configured metric and dimension names. `export: {}` defaults to
100 records; `limit` must be a positive integer. Passed rules skip their export.
Schema-contract failures skip all rule evaluation and raw exports. No export is
enabled by default, and `--fail-on-rule-violation` is not needed to generate one.

Each requested non-passing rule writes `rule_N_rows.csv`, where N is its zero-based
position in the configuration. Rule names never become filenames. Rows sort by
configured key columns for deterministic limits. Exports contain the joined
selected fields (`kN`, `rv`/`cv`, `rdN`/`cdN`, `rfN`/`cfN`, `rp`/`cp`); unlike the
general focused exports, these files do not add a contribution column. A field
rule uses the first metric's joined table for metric context and exports once,
regardless of how many metrics are configured.

| Rule measure | Supporting records |
| --- | --- |
| Field count or percentage | Matched records counted by that measure (the numerator for a percentage) |
| Overall removals / removal percentage | Reference-only keys |
| Overall additions | Current-only keys |
| Current total or current row count | Keys present in the current snapshot |
| Delta or percentage change | All keys contributing to the before/after population |
| Segment rule | The corresponding population restricted to failed or undefined selected segments; removals/additions include moves out/in |

All failing segments are considered, including those outside the report's
50-segment display limit. Keys appearing in several failing segments are exported
once per rule. Records are supporting evidence, not individually proven causes
or violations. A lower-bound failure can produce a header-only file when no
existing records qualify; the export cannot invent missing records. A percentage
export contains numerator records, not the full denominator population.

`rule_checks.results[N].evidence_export` links each rule to its file and records
`rows`, `total_rows`, `limit`, `truncated`, the selection description, and a SQL
evidence view. Exported files also appear in the normal report/manifest inventory.
HTML includes a relative download link, and CLI output shows counts and truncation.
Passed rules instead record a skipped status and reason.

Replay creates `rule_N_evidence_all` (all qualifying rows) and `rule_N_evidence`
(the limited selection) in the applicable metric schema. Segment exports also
record `rule_N_failed_segments` with IDs into `segment_rule_N`; those IDs preserve
this run's Python rule decisions, rather than reevaluating thresholds during SQL
replay. Replay does not write CSV files. SQL and manifests remain local to the
standalone investigation, and failed exports leave a prior bundle untouched.

### Category movement tables

Each configured dimension and dimension group automatically includes a movement
table for every metric. A transition shows its original and new categories,
number of matched keys, how many also changed metric value, and the before/after
metric amounts. Additions, removals, unchanged categories, and metric-only changes
are excluded. For count metrics, both amounts equal the moved record count.

Tables rank by moved record count descending, then source and destination
categories for deterministic ties. The top 50 transitions are retained, followed
by an explicit Other remainder when needed. Null categories remain distinct from
literal text and Other; combined categories retain their component types.
Displayed row counts and before/after amounts are checked against the existing
reclassification totals, using the existing tolerance for floating-point amounts.
Before/after amounts describe the moved population, not additional contributions
to the overall delta. A key can appear in several dimension/group tables; do not
add those tables together. A change in category does not establish why it happened.

In JSON and the Python API, movement details live at
`findings["metrics"][i]["reclassifications"][j]["movements"]`, with `columns`,
`rows`, amount `checks`, and a qualified `evidence` table name. The legacy
top-level `reclassifications` exposes the first metric's tables too. SQL replay
materializes the complete `movement_N` tables and bounded `movement_N_display`
tables in each metric's schema; display order is `ORDER BY rank`.

To export underlying moved records, explicitly opt in:

```yaml
report:
  evidence_exports:
    - kind: moved
      limit: 100
```

This writes `moved_rows.csv` (or `metric_0_moved_rows.csv`, etc. for multiple
metrics). It contains each qualifying key once per metric, even if several
dimensions changed, with before/after selected fields and metric contribution.
Like other focused exports, it sorts by absolute metric contribution descending,
then key; omit `limit` to export all moved records. Zero-contribution category
moves are included. With no selected dimensions or no moves, the file has only
a header. Aggregate movement tables never include raw keys; raw export remains
opt-in. The selection is replayable as `evidence_moved` in each metric's schema.

### Threshold rules

Add optional `rules` to a comparison configuration to flag changes that require
review. Each rule targets a configured metric by name and has an inclusive `min`,
`max`, or both. For example, using the `net_sales` and `orders` metrics above:

```yaml
rules:
  - name: Sales change within five percent
    metric: net_sales
    measure: abs_percent_change
    max: 5
  - name: At most one percent of orders removed
    metric: orders
    measure: removed_percent
    max: 1
  - name: Expected order count
    metric: orders
    measure: current_rows
    min: 3
    max: 10
```

| Measure | Observed value |
| --- | --- |
| `delta` | Signed current total minus reference total |
| `abs_delta` | Magnitude of that total change, in metric units |
| `percent_change` | Signed `100 * delta / reference_total` |
| `abs_percent_change` | Magnitude of percentage change |
| `current_total` | Current metric total (sum or count) |
| `current_rows` | Number of keys in the current snapshot |
| `added_rows` | Number of current-only keys |
| `removed_rows` | Number of reference-only keys |
| `removed_percent` | `100 * removed_rows / reference_rows` |

Percentages use percentage points: `max: 5` means 5%, not 0.05%. Signed percentage
change retains the sign of the reference total; use `abs_percent_change` to bound
the magnitude regardless of direction. Percentage change is undefined when the
reference total is zero, even when both totals are zero. Removal percentage is
undefined when the reference has no rows. Undefined rules do not pass.

Rules appear in the CLI, HTML report, and `rule_checks` in both `findings.json`
and `manifest.json`. Each result includes the observed value, configured bounds,
status (`passed`, `failed`, or `undefined`), reason when applicable, and evidence
table. The overall status is `not_configured` for no rules, `passed` when every
rule passes, and `failed` when any rule fails or is undefined. A policy failure
does not change arithmetic reconciliation or the manifest's successful execution
status, and the full evidence bundle is still published.

By default, rule failures are informational. To use the rules as a local or
external pipeline check:

```sh
delta-detective investigate comparison.yaml --out investigation --fail-on-rule-violation
```

Exit codes are 0 for successful execution (and, with the flag, passing or absent
rules), 2 for configuration/input/execution errors, and 3 for failed or undefined
rules when the flag is set, and 4 for schema contract violations. The Python API returns completed results on
policy failure; inspect `findings["rule_checks"]`. No scheduler or GitHub Actions
workflow is installed or required.

Rule names must be unique and nonempty. Bounds must be finite numbers or numeric
strings, with `min <= max`. Quote high-precision decimal bounds, for example
`max: "0.00000000000000000001"`, to avoid YAML floating-point rounding. Comparisons
use decimal representations of observed results; percentages use the same
100-digit decimal calculation precision as the report. FLOAT/DOUBLE measurements
remain approximate. Rule bounds have no implicit tolerance; arithmetic
reconciliation tolerance does not relax policy limits. Rules flag observed
changes, not proven errors or operational causes. SQL replay reproduces the
underlying measurements; it does not reevaluate Python threshold rules.

### Segment-level threshold rules

Add `group_by` to apply a rule independently to every category in a configured
dimension or combined dimension group. It must match the configured column list
and order exactly. An optional `where` selects one exact segment by specifying
every group column. For example:

```yaml
dimensions: [region, source]
dimension_groups: [[region, source]]
rules:
  - name: Every region stays within ten percent
    metric: net_sales
    group_by: [region]
    measure: abs_percent_change
    max: 10
  - name: Each source retains ninety-five percent of its records
    metric: orders
    group_by: [source]
    measure: removed_percent
    max: 5
  - name: West paid search sales floor
    metric: net_sales
    group_by: [region, source]
    where: {region: West, source: paid search}
    measure: current_total
    min: 1000
```

Segment rules support the same measures and inclusive bounds as overall rules.
Totals and row counts use each snapshot's own category membership. `removed_rows`
counts reference keys that disappeared **or moved out of that segment**;
`added_rows` counts new keys **or keys that moved into that segment**.
`removed_percent` divides segment removals by the reference segment row count.
It therefore measures loss of the original segment membership even when new
records replace it. Overall removal rules still count only keys absent from the
current dataset.

Every actual segment present in either snapshot is checked, including those
hidden by the breakdown report's Other remainder. No rule is evaluated against
Other as an aggregate category. New segments with zero reference totals have
undefined percentage change; disappeared segments have zero current totals.
A zero reference row count makes removal percentage undefined. An empty dataset
or a `where` selector matching neither snapshot also produces an undefined rule.
Undefined results cause the overall rule check to fail and, with
`--fail-on-rule-violation`, return exit code 3 after writing the report.

Use YAML `null` to select an actual null category, quoted text for string
categories, and booleans for boolean categories. Numeric categories accept finite
numbers or quoted exact decimals; selectors are compared without rounding to the
column's scale. For example, `"1.251"` does not select decimal category `1.25`.

HTML, CLI, and JSON show per-segment observations and outcomes. Each segment rule
has `segment_counts`, `total_segments`, `segments`, and `omitted_segments` in
`rule_checks.results`. Up to 50 results are displayed, with failed segments first,
undefined next, and passed last; categories determine order within each status.
Counts always cover the complete selected population, not just displayed rows.
SQL replay creates complete aggregate `segment_rule_N` tables in the metric's
schema, where N is the rule's zero-based position in configuration; `g0`, `g1`,
etc. follow `group_by` order. These tables include all actual segments before the
optional Python selector and threshold evaluation. Source keys are not exposed.

Choose segment scopes through the expanded `init` options or directly in YAML.
Each investigation remains standalone and uses only its configured
reference and current snapshots. No history store or GitHub Actions is needed.

Selected columns must have exactly matching DuckDB types across snapshots.
Floating-point and nested key types are rejected. Dimensions accept strings,
booleans, and exact numeric categorical codes. Nonselected schema changes are
reported but do not block comparison. Every key component must be non-null and
every key tuple unique. Duplicate keys block analysis before any join.

CSV defaults to DuckDB full-file schema sniffing (`sample_size=-1`), then a strict read
with explicit inferred columns, delimiter, quote/escape, header, date/time formats,
no ignored errors, no null padding, no comments, and empty fields interpreted as
null. Optional parsing overrides are described below. Leading-row skipping is rejected. The reader uses DuckDB's default newline
recognition; the manifest also records the sniffer's observed newline. Explicit
sniffer newline settings interact incorrectly with strict mode in DuckDB 1.5.5,
so they are deliberately not passed to the reader. Header-only CSV columns may
infer incompatible types; use explicit CSV types or typed empty Parquet files for empty snapshots.
Malformed structure or unsuitable numeric inference fails with an actionable
error. Numeric-looking identifiers can lose formatting during CSV inference;
use explicit `VARCHAR` CSV types or typed Parquet to preserve identifier semantics. No source data is repaired.

### Optional CSV parsing overrides

Add a `csv` section to the YAML configuration to override parsing separately for
each input. Omitted settings retain automatic inference and existing defaults.
For example, preserve leading zeros in IDs and read amounts directly as decimals:

```yaml
csv:
  reference:
    header: true
    delimiter: ";"
    types:
      order_id: VARCHAR
      net_amount: DECIMAL(18,2)
  current:
    header: true
    types:
      order_id: VARCHAR
      net_amount: DECIMAL(18,2)
```

Each side accepts `types` (a nonempty mapping of column names to DuckDB type
strings), `delimiter`, `header` (boolean), `quote`, `escape`, `nullstr`, `dateformat`,
and `timestampformat`. The delimiter must be one ASCII character; quote and escape
must be one ASCII character or empty. Format strings use DuckDB conventions, for
example `dateformat: "%d/%m/%Y"`. The default null marker is empty text;
`nullstr: "NA"` instead treats `NA` as null. Quote YAML strings explicitly when
they could otherwise be interpreted as booleans or null.

Types apply during CSV reading, before comparison; unspecified columns are
inferred. Unknown column names and invalid options fail the run. With
`header: false`, use DuckDB's generated names such as `column0` and `column1`
(wide files may use zero-padded names). Set `header: true` and types for every
selected column when comparing a header-only CSV with a populated snapshot.
Overrides on Parquet inputs are rejected. Schema contracts remain separate checks
of the resulting loaded types.

The manifest records the requested overrides and effective parsing settings;
replay SQL uses those effective settings. Strict row validation remains enabled.
DuckDB's memory, thread, and other resource settings remain unchanged. The init
wizard defaults to automatic CSV inference; its optional input setup accepts
overrides before loading. Overrides can also be added directly to YAML.

## Interpretation and arithmetic

The full outer join uses each actual key column, never concatenated strings or
hash-only identity. For sums:

```
current total - reference total
  = current-only amounts
  - reference-only amounts
  + sum(current amount - reference amount) for matched keys
```

For row counts, additions contribute +1, removals -1, and matches zero. Both
key-count identities and reconciliation must pass. Empty populations are labeled
and contribute zero. A zero reference total has undefined percentage change;
otherwise percentage change is `100 * delta / reference_total`, including signed
reference totals. Absolute change is always shown.

Integers use HUGEINT arithmetic and decimals widen to DECIMAL(38, input scale).
Overflow fails explicitly; no rounding to float is used. JSON decimals are strings
to preserve precision; integers remain JSON integers (consumers must preserve
large integer precision). FLOAT/DOUBLE use DOUBLE calculations and are labeled
approximate. Reconciliation checks use:

```
abs(residual) <= max(1e-9,
    1e-12 * max(abs(reference_total), abs(current_total), sum(abs(contributions))))
```

This is an arithmetic tolerance, not statistical confidence. Severe cancellation
can fail the check; use exact decimal inputs where exact equality matters. The
tool cannot restore precision already lost in upstream files.

Each dimension independently groups the original snapshot values. A matched key
moving categories contributes its old amount to its old category and its new
amount to its new category. Reclassification summaries count these records and
separately count those that also changed metric. These populations may overlap
between dimensions. **Never add contributions across dimension tables.**
Tables rank by absolute contribution and retain signs. More than 50 categories
produce an explicit Other remainder; actual null categories remain distinct from
literal strings such as `(NULL)` and from the remainder.

Only selected metrics, dimensions, and `compare_fields` are compared. No claim that an entire row
is unchanged is made. An observed dataset difference is not necessarily an
operational failure or a real-world change. Absent categories alone do not prove
missing loads. The tool establishes differences and arithmetic contributions,
not causes or automatic root-cause analysis.

## Evidence and privacy

The bundle contains:

| File | Contents |
| --- | --- |
| `report.html` | Standalone report built from the same structured results as JSON; escaped data-derived markup |
| `findings.json` | Population findings, reconciliation, checks, dimensions, reclassifications, limitations |
| `analysis.sql` | Actual executed loading, validation, joins and aggregation, plus result queries |
| `manifest.json` | Resolved config, SHA-256 fingerprints, schemas, parsing decisions, app/DuckDB versions, status and limitations |

Run `analysis.sql` in a **fresh DuckDB database** with the matching inputs at the
recorded absolute paths. Input datasets are not copied into the bundle. Replay
materializes `reconciliation`, `dimension_N`, `dimension_N_display`, and
`reclassification_N`; the complete dimension table remains queryable even when
the report is truncated. For multiple metrics, the first metric uses the `main`
schema and subsequent metrics use `metric_1`, `metric_2`, etc. Use qualified names
such as `metric_1.reconciliation`. Requested focused selections are replayable as
`evidence_removed`, `evidence_largest_changes`, etc., in the corresponding schema;
replay does not write CSV exports. Python checks the returned residuals and identities
before marking a bundle successful. Replay SQL reproduces the measurements;
it does not regenerate the HTML/JSON or recheck checksums. The manifest's hashes
allow independently verifying that the same input bytes were used.

Raw rows and key values are excluded by default. `include_raw_rows: true` adds a
streamed `raw_rows.csv` containing **all joined keys and selected fields**, not all
source columns. `kN` follows configured key order; `rv`/`cv` are reference/current
metric values (`1` for count); `rdN`/`cdN` follow dimension order, then any additional
group columns in first-use order (recorded as `raw_evidence.selected_dimensions`
in the manifest); `rp`/`cp` indicate
side presence. Missing-side fields are empty. CSV null and empty-text rendering
can be ambiguous; use the replay database for typed evidence. Aggregate-only
reports are **not anonymous**: category values, totals, schemas and input paths
can still be sensitive. No arbitrary confidence percentages are generated.

## Synthetic demonstration

The demo writes deterministic typed Parquet inputs and a runnable config:

| Planted change | Contribution |
| --- | ---: |
| Order 5 added with 2,000 | +2,000 |
| Order 4 removed with 14,000 | -14,000 |
| Order 1 changes from 40,000 to 34,000 | -6,000 |
| Order 2 moves West → South; amount stays 30,000 | 0 |
| Order 3 stays at 16,000 | 0 |

Reference 100,000; current 82,000; change -18,000 (-18%); exact residual zero.
These are planted synthetic changes, not observed business events.

## Python API and scope

```python
from delta_detective import investigate
findings = investigate("demo-data/comparison.yaml", "investigation")
print(findings["summary"]["delta"])
```

`findings["metrics"]` contains one result per configured metric, including its
definition, summary, findings, dimensions, reclassifications, and SQL schema.
Legacy top-level summary/findings/dimensions/reclassifications and the manifest's
`reconciliation` describe the first metric. The manifest's `metric_reconciliations`
contains all metric checks. `findings["evidence_exports"]` lists raw exports.

Modules separate configuration, loading/profiling, comparison, findings,
reporting, CLI, and demo generation. Dependencies are DuckDB, PyYAML and Jinja2;
configuration validation is intentionally small and explicit, and the CLI uses
stdlib argparse. Bulk data remains inside DuckDB; disk and memory requirements
still scale with input size. This release does not include performance benchmarks
or configurable resource limits. Tests use independently specified expectations
and exercise SQL replay and the CLI.

Only snapshot count/sum comparison is implemented. No periods, rates,
percentiles, causal inference, AI explanations, drift models, cloud connectors,
accounts, scheduler, or web application are included.

## License

MIT License. See [LICENSE](LICENSE).
