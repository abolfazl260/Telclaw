"""Local Advertio reports. Run with python -m scripts.advertio_review."""

import argparse
import json

from delivery.advertio_cargo_review import review_history, route_diagnostics
from delivery.advertio_lifecycle import duplicate_report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", choices=("cargo-history", "cargo-routes", "duplicates"))
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--after", type=int, default=0)
    parser.add_argument("--apply", nargs="+", type=int, default=(), metavar="LOCAL_ID")
    args = parser.parse_args()
    if args.apply and args.report != "cargo-history":
        parser.error("--apply is supported only for cargo-history")
    try:
        if args.report == "cargo-history":
            data = review_history(after=args.after, limit=args.limit, apply_ids=args.apply)
        elif args.report == "cargo-routes":
            data = route_diagnostics(after=args.after, limit=args.limit)
        else:
            data = duplicate_report(limit=args.limit)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(data, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
