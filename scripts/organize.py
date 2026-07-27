"""Record the agent's analysis and lay the PDFs out in category subfolders.

The topic folder is the visible artefact, so classification is physical: each
paper ends up in `<topic>/<Category>/`. That also means the folder can be the
input as well as the output -- drag a PDF into a different category folder and
`--adopt-folders` will pick the change up.

The taxonomy is per topic. It is meant to be designed after reading the whole
digest, not guessed in advance, and it is expected to be revised.

Usage:
    python organize.py --topic <dir> --set-taxonomy '{"categories": [...]}'
    python organize.py --topic <dir> --show-taxonomy
    python organize.py --topic <dir> --assign '[{"key": "...", "category": "..."}]'
    python organize.py --topic <dir> --adopt-folders   # disk -> sidecar
    python organize.py --topic <dir> --sync            # sidecar -> disk
"""
import argparse
import json

from common import (Sidecar, add_topic_arg, print_json, sanitize_dirname,
                    scan_pdfs)

ASSIGN_FIELDS = ("category", "tags", "summary", "key_contributions", "notes")


def apply_layout(sidecar: Sidecar, dry_run: bool) -> list[dict]:
    """Move each PDF under the folder named after its category."""
    moves = []
    for key in sorted(sidecar.entries):
        rec = sidecar.entries[key]
        category = rec.get("category")
        current = rec.get("path")
        if not current:
            continue
        source = sidecar.topic / current
        target_rel = ("%s/%s" % (sanitize_dirname(category), key)) if category else key
        if current == target_rel:
            continue
        target = sidecar.topic / target_rel
        moves.append({"key": key, "from": current, "to": target_rel})
        if dry_run:
            continue
        if not source.exists():
            moves[-1]["error"] = "source file missing"
            continue
        if target.exists():
            moves[-1]["error"] = "target already exists, skipped"
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        source.rename(target)
        rec["path"] = target_rel

    if not dry_run:
        for path in sorted(sidecar.topic.iterdir()):
            if path.is_dir() and not path.name.startswith("."):
                if not any(path.rglob("*.pdf")):
                    try:
                        path.rmdir()
                    except OSError:
                        pass
    return moves


def cmd_set_taxonomy(sidecar: Sidecar, raw: str):
    data = json.loads(raw)
    categories = data.get("categories") if isinstance(data, dict) else data
    if not isinstance(categories, list) or not categories:
        raise SystemExit("taxonomy must contain a non-empty 'categories' list")
    clean = []
    for entry in categories:
        if isinstance(entry, str):
            entry = {"name": entry, "description": ""}
        if not entry.get("name"):
            raise SystemExit("every category needs a 'name'")
        clean.append({"name": entry["name"].strip(),
                      "description": (entry.get("description") or "").strip()})
    names = [c["name"] for c in clean]
    if len(set(names)) != len(names):
        raise SystemExit("duplicate category names: %s" % names)
    sidecar.taxonomy = {"categories": clean,
                        "notes": (data.get("notes") if isinstance(data, dict) else None) or ""}
    sidecar.save_taxonomy()
    print_json({"categories": names, "path": str(sidecar.taxonomy_path)})


def cmd_assign(sidecar: Sidecar, raw: str, allow_new: bool, dry_run: bool, move: bool):
    payload = json.loads(raw)
    if isinstance(payload, dict):
        payload = [payload]

    known = set(sidecar.category_names())
    applied, rejected, new_categories = [], [], []

    for item in payload:
        key = item.get("key")
        if not key:
            rejected.append({"item": item, "reason": "missing 'key'"})
            continue
        if key not in sidecar.entries:
            rejected.append({"key": key, "reason": "not in this topic; run collect.py"})
            continue
        category = item.get("category")
        if category and category not in known:
            if not allow_new:
                rejected.append({"key": key, "reason":
                                 "category %r is not in the taxonomy; set it first "
                                 "or pass --allow-new" % category})
                continue
            known.add(category)
            new_categories.append(category)

        fields = {f: item[f] for f in ASSIGN_FIELDS if f in item}
        if not dry_run:
            sidecar.upsert(key, **fields)
        applied.append({"key": key, "category": category})

    if new_categories and not dry_run:
        existing = sidecar.taxonomy.setdefault("categories", [])
        for name in dict.fromkeys(new_categories):
            existing.append({"name": name, "description": ""})
        sidecar.save_taxonomy()

    moves = apply_layout(sidecar, dry_run) if move else []
    if not dry_run:
        sidecar.save()

    print_json({
        "dry_run": dry_run,
        "assigned": len(applied),
        "rejected": rejected,
        "new_categories_created": sorted(set(new_categories)),
        "moves": moves,
        "uncategorized": sorted(k for k, r in sidecar.entries.items()
                                if not r.get("category")),
    })


def cmd_adopt_folders(sidecar: Sidecar, dry_run: bool):
    """Take the on-disk folder names as the truth for category."""
    papers, problems = scan_pdfs(sidecar.topic)
    changes = []
    for paper in papers:
        rec = sidecar.entries.get(paper["key"])
        if rec is None:
            changes.append({"key": paper["key"], "reason": "not collected yet"})
            continue
        folder = paper["category_dir"]
        if rec.get("category") != folder:
            changes.append({"key": paper["key"], "from": rec.get("category"), "to": folder})
            if not dry_run:
                sidecar.upsert(paper["key"], category=folder, path=paper["path"])
        elif not dry_run:
            sidecar.upsert(paper["key"], path=paper["path"])

    if not dry_run:
        for name in dict.fromkeys(
                c["to"] for c in changes if c.get("to")):
            if name not in sidecar.category_names():
                sidecar.taxonomy.setdefault("categories", []).append(
                    {"name": name, "description": ""})
        sidecar.save_taxonomy()
        sidecar.save()

    print_json({"dry_run": dry_run, "changes": changes, "problems": problems})


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    add_topic_arg(ap)
    ap.add_argument("--set-taxonomy", metavar="JSON")
    ap.add_argument("--show-taxonomy", action="store_true")
    ap.add_argument("--assign", metavar="JSON",
                    help="list of {key, category, tags, summary, key_contributions}")
    ap.add_argument("--adopt-folders", action="store_true",
                    help="read categories back from the on-disk folder names")
    ap.add_argument("--sync", action="store_true",
                    help="re-apply the sidecar's categories to the folder layout")
    ap.add_argument("--allow-new", action="store_true",
                    help="let --assign introduce categories not yet in the taxonomy")
    ap.add_argument("--no-move", action="store_true",
                    help="update the sidecar without touching the folder layout")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    sidecar = Sidecar(args.topic)

    if args.set_taxonomy:
        cmd_set_taxonomy(sidecar, args.set_taxonomy)
    if args.show_taxonomy:
        print_json(sidecar.taxonomy)
    if args.assign:
        cmd_assign(sidecar, args.assign, args.allow_new, args.dry_run,
                   move=not args.no_move)
    if args.adopt_folders:
        cmd_adopt_folders(sidecar, args.dry_run)
    if args.sync:
        moves = apply_layout(sidecar, args.dry_run)
        if not args.dry_run:
            sidecar.save()
        print_json({"dry_run": args.dry_run, "moves": moves})

    if not any((args.set_taxonomy, args.show_taxonomy, args.assign,
                args.adopt_folders, args.sync)):
        ap.error("pick one of --set-taxonomy / --show-taxonomy / --assign / "
                 "--adopt-folders / --sync")


if __name__ == "__main__":
    main()
