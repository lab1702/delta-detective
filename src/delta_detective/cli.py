import argparse
import sys
from .config import InvestigationError
from .core import investigate
from .demo import demo
from .wizard import init_config


def main(argv=None):
    parser = argparse.ArgumentParser(description="Reconcile two local versions of one logical dataset.")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Interactively create a validated comparison configuration.")
    init.add_argument("reference")
    init.add_argument("current")
    init.add_argument("--out", default="comparison.yaml", help="New YAML file (default: comparison.yaml).")
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
        else:
            data = investigate(args.config, args.out, args.overwrite)
            for item in data["metrics"]:
                result = item["summary"]
                print(f"Metric: {item['metric']['name']}")
                print(f"Reference: {result['reference_total']}\nCurrent: {result['current_total']}\nAbsolute change: {result['delta']}")
                print("Relative change: " + (f"{result['percent_change']}%" if result['percent_change'] is not None else "undefined (zero reference)"))
                for kind in ("added", "removed", "matched"):
                    print(f"{kind}: {result[kind+'_rows']} rows; contribution {result[kind+'_contribution']}")
                print(f"Reconciliation: {result['status']} ({'exact' if result['exact'] else 'approximate'}), residual {result['residual']}")
            print(f"Report: {args.out}/report.html")
            print(f"Threshold rules: {data['rule_checks']['status']}")
            for rule in data["rule_checks"]["results"]:
                bounds = ", ".join(f"{key}={rule[key]}" for key in ("min", "max") if key in rule)
                observed = rule['observed'] if rule['observed'] is not None else 'undefined'
                print(f"{rule['name']}: {rule['status']} ({rule['metric']}.{rule['measure']}={observed}; {bounds})")
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
