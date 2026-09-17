import argparse
import sys
from .config import InvestigationError
from .core import investigate
from .demo import demo
from .wizard import init_config
from .validation import validate


def main(argv=None):
    parser = argparse.ArgumentParser(description="Reconcile two local versions of one logical dataset.")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Interactively create a validated comparison configuration.")
    init.add_argument("reference")
    init.add_argument("current")
    init.add_argument("--out", default="comparison.yaml", help="New YAML file (default: comparison.yaml).")
    validation = commands.add_parser('validate', help='Diagnose input problems without running a comparison.')
    validation.add_argument('config')
    validation.add_argument('--out', required=True)
    validation.add_argument('--overwrite', action='store_true', help='Replace an existing validation bundle only.')
    validation.add_argument('--export-invalid-rows', action='store_true', help='Export keys and offending selected fields for failed row checks.')
    validation.add_argument('--limit', type=int, default=100, help='Maximum exported rows per failed check (default: 100).')
    for name in ("demo", "investigate"):
        sub = commands.add_parser(name)
        if name == "investigate":
            sub.add_argument("config")
            sub.add_argument("--fail-on-rule-violation", action="store_true",
                             help="Exit 3 after writing the bundle if a rule fails or is undefined.")
        sub.add_argument("--out", required=True)
        sub.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            init_config(args.reference, args.current, args.out)
        elif args.command == "demo":
            print(f"Demo configuration: {demo(args.out, args.overwrite)}")
        elif args.command == 'validate':
            data = validate(args.config, args.out, args.overwrite, args.export_invalid_rows, args.limit)
            print(f"Input validation: {data['status']}")
            for check in data['checks']:
                if check['status'] == 'failed':
                    print(f"{check['side']}: {check['check']} ({', '.join(check['columns'])}): {check['details']}")
            print(f'Validation report: {args.out}/report.html')
            return 4 if data['status'] == 'failed' else 0
        else:
            data = investigate(args.config, args.out, args.overwrite)
            print(f"Schema contract: {data['schema_checks']['status']}")
            if data['schema_checks']['status'] == 'failed':
                for check in data['schema_checks']['results']:
                    if check['status'] == 'failed':
                        print(f"{check['side']}.{check['column']}: {check['issue']}; expected={check['expected_type']}, observed={check['observed_type']}")
                print(f"Comparison not run. Report: {args.out}/report.html")
                return 4
            for item in data["metrics"]:
                result = item["summary"]
                print(f"Metric: {item['metric']['name']}")
                print(f"Reference: {result['reference_total']}\nCurrent: {result['current_total']}\nAbsolute change: {result['delta']}")
                print("Relative change: " + (f"{result['percent_change']}%" if result['percent_change'] is not None else "undefined (zero reference)"))
                for kind in ("added", "removed", "matched"):
                    print(f"{kind}: {result[kind+'_rows']} rows; contribution {result[kind+'_contribution']}")
                print(f"Reconciliation: {result['status']} ({'exact' if result['exact'] else 'approximate'}), residual {result['residual']}")
            print(f"Report: {args.out}/report.html")
            for field in data['field_changes']['fields']:
                print(f"Field {field['name']}: {field['changed_rows']} of {field['matched_rows']} matched records changed; became null={field['became_null']}; from null={field['from_null']}")
            print(f"Threshold rules: {data['rule_checks']['status']}")
            for rule in data["rule_checks"]["results"]:
                bounds = ", ".join(f"{key}={rule[key]}" for key in ("min", "max") if key in rule)
                observed = rule['observed'] if rule['observed'] is not None else 'undefined'
                target = rule.get('field', rule.get('metric'))
                print(f"{rule['name']}: {rule['status']} ({target}.{rule['measure']}={observed}; {bounds})")
                if 'field' in rule:
                    print(f"  {rule['numerator']} of {rule['denominator']} matched records; unit={rule['unit']}")
                if 'evidence_export' in rule:
                    export = rule['evidence_export']
                    if export['status'] == 'exported':
                        print(f"  Evidence: {export['file']}; {export['rows']} of {export['total_rows']} rows; truncated={export['truncated']}")
                    else:
                        print(f"  Evidence skipped: {export['reason']}")
                if 'segment_counts' in rule:
                    print(f"  Segments: {rule['segment_counts']}; omitted from display: {rule['omitted_segments']}")
                    for segment in rule['segments']:
                        print(f"  {segment['segment']}: {segment['status']}; observed={segment['observed']}")
            if args.fail_on_rule_violation and data["rule_checks"]["status"] == "failed":
                return 3
        return 0
    except (InvestigationError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
