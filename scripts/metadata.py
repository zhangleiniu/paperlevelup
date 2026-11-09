"""Create, validate, and apply evidence-bearing topic metadata overrides."""

import argparse
import json
from pathlib import Path

from common import Sidecar, add_topic_arg, print_json
from metadata_overrides import (apply_overrides, load_overrides,
                                normalize_entries, overrides_path,
                                remove_entries, save_overrides, set_entries,
                                validate_ledger)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    add_topic_arg(ap)
    ap.add_argument("--set", dest="set_json",
                    help="JSON object/list; keys are exact PDF basenames")
    ap.add_argument("--file", type=Path,
                    help="read override JSON from a file instead of --set")
    ap.add_argument("--remove", nargs="+", metavar="PDF_BASENAME",
                    help="remove complete override records")
    ap.add_argument("--apply", action="store_true",
                    help="apply the existing ledger to papers.jsonl")
    ap.add_argument("--check", action="store_true",
                    help="validate and report the ledger without writing")
    args = ap.parse_args()

    if args.set_json and args.file:
        raise SystemExit("use one of --set or --file, not both")
    sidecar = Sidecar(args.topic)
    data = load_overrides(sidecar)
    changed = []
    removed = []

    if args.set_json or args.file:
        raw = (args.file.read_text(encoding="utf-8") if args.file
               else args.set_json)
        changed = set_entries(sidecar, data, normalize_entries(json.loads(raw)))
    if args.remove:
        removed = remove_entries(data, args.remove)

    mutating_ledger = bool(args.set_json or args.file or args.remove)
    if mutating_ledger:
        save_overrides(sidecar, data)

    validate_ledger(data, set(sidecar.entries))
    applied = []
    if mutating_ledger or args.apply:
        applied = apply_overrides(sidecar, data)
        sidecar.save()

    print_json({
        "topic": str(sidecar.topic),
        "path": str(overrides_path(sidecar)),
        "records": len(data.get("records", {})),
        "changed": changed,
        "removed": removed,
        "applied": applied,
        "check": "ok",
    })


if __name__ == "__main__":
    main()
