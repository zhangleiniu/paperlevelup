"""Write TOPIC.md: the readable view of a topic, grouped by category.

Links are relative to the topic folder, so they open the actual PDF when the
folder is the workspace root in VS Code.

Usage:
    python report.py --topic <dir>
    python report.py --topic <dir> --out ~/somewhere/else.md
"""
import argparse
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote

from common import Sidecar, add_topic_arg, now_iso


def render(sidecar: Sidecar) -> str:
    records = list(sidecar.entries.values())
    by_category = defaultdict(list)
    for rec in records:
        by_category[rec.get("category") or "Uncategorized"].append(rec)

    ordered = [c["name"] for c in sidecar.taxonomy.get("categories", [])
               if c["name"] in by_category]
    ordered += sorted(c for c in by_category if c not in ordered and c != "Uncategorized")
    if "Uncategorized" in by_category:
        ordered.append("Uncategorized")

    in_library = sum(1 for r in records if r.get("archer_id"))
    lines = [
        "# %s" % sidecar.topic.name,
        "",
        "%d papers in %d categories · %d resolved against the library, "
        "%d local only · updated %s" % (
            len(records), len([c for c in ordered if c != "Uncategorized"]),
            in_library, len(records) - in_library, now_iso()),
        "",
    ]

    for category in ordered:
        papers = sorted(by_category[category],
                        key=lambda r: (-(r.get("year") or 0), r.get("title") or ""))
        description = next((c.get("description") for c in
                            sidecar.taxonomy.get("categories", [])
                            if c["name"] == category), None)
        lines.append("## %s (%d)" % (category, len(papers)))
        if description:
            lines.append("")
            lines.append("_%s_" % description)
        lines.append("")

        for rec in papers:
            title = rec.get("title") or rec["key"]
            link = quote(rec.get("path") or rec["key"])
            lines.append("### [%s](%s)" % (title.replace("]", ")"), link))
            byline = ", ".join((rec.get("authors") or [])[:6])
            if len(rec.get("authors") or []) > 6:
                byline += " et al."
            venue_year = " · ".join(str(x) for x in
                                    (rec.get("venue"), rec.get("year")) if x)
            meta = " — ".join(p for p in (byline and "*%s*" % byline, venue_year) if p)
            if meta:
                lines.append(meta)
            if rec.get("summary"):
                lines.extend(["", rec["summary"]])
            if rec.get("key_contributions"):
                lines.extend(["", "**Contributions**"])
                lines.extend("- %s" % c for c in rec["key_contributions"])
            if rec.get("notes"):
                lines.extend(["", "> %s" % rec["notes"]])
            if rec.get("tags"):
                lines.extend(["", "`%s`" % "` `".join(rec["tags"])])
            lines.append("")

    stale = [r["key"] for r in records
             if not r.get("summary") and not r.get("category")]
    if stale:
        lines.extend(["## Not yet analysed", ""])
        lines.extend("- %s" % k for k in sorted(stale))
        lines.append("")

    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    add_topic_arg(ap)
    ap.add_argument("--out", default=None,
                    help="defaults to <topic>/TOPIC.md")
    args = ap.parse_args()

    sidecar = Sidecar(args.topic)
    if not sidecar.entries:
        raise SystemExit("nothing collected yet; run collect.py first")

    out = Path(args.out).expanduser() if args.out else sidecar.topic / "TOPIC.md"
    out.write_text(render(sidecar), encoding="utf-8")
    print("wrote %s (%d papers)" % (out, len(sidecar.entries)))


if __name__ == "__main__":
    main()
