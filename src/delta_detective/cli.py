import argparse
import sys
from .config import InvestigationError
from .core import investigate
from .demo import demo


def main(argv=None):
    parser = argparse.ArgumentParser(description="Reconcile two local versions of one logical dataset.")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("demo", "investigate"):
        sub = commands.add_parser(name)
        if name == "investigate":
            sub.add_argument("config")
        sub.add_argument("--out", required=True)
        sub.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "demo":
            print(f"Demo configuration: {demo(args.out, args.overwrite)}")
        else:
            result = investigate(args.config, args.out, args.overwrite)["summary"]
            print(f"Reference: {result['reference_total']}\nCurrent: {result['current_total']}\nAbsolute change: {result['delta']}")
            print("Relative change: " + (f"{result['percent_change']}%" if result['percent_change'] is not None else "undefined (zero reference)"))
            for kind in ("added", "removed", "matched"):
                print(f"{kind}: {result[kind+'_rows']} rows; contribution {result[kind+'_contribution']}")
            print(f"Reconciliation: {result['status']} ({'exact' if result['exact'] else 'approximate'}), residual {result['residual']}\nReport: {args.out}/report.html")
        return 0
    except (InvestigationError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
