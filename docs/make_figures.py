"""Render the README's figures: the architecture diagram and the example chats.

Edit the boxes or the `folder` and `lookup` conversations below, then run
`python3 docs/make_figures.py`.
"""
import sys
from html import escape
from pathlib import Path

FONT = "ui-monospace, SFMono-Regular, Menlo, Consolas, 'Liberation Mono', monospace"
SIZE = 13.5
CHAR_W = SIZE * 0.602
LINE_H = 21
PAD_X = 26
WIDTH = 780

COLORS = {
    "bg": "#1d1e22", "bar": "#2a2c31", "border": "#3a3c42",
    "text": "#d4d6db", "dim": "#8b9099", "user": "#f3f4f6", "userbg": "#30333a",
    "bullet": "#e0915e", "head": "#9db4f0", "plus": "#7cc994", "minus": "#e27d7d",
    "title": "#a3a7af",
}


def tspan(text, color=None, weight=None):
    attrs = f' fill="{COLORS[color]}"' if color else ""
    if weight:
        attrs += f' font-weight="{weight}"'
    return f"<tspan{attrs}>{escape(text)}</tspan>"


def render(title, turns, out):
    body, y = [], 52 + 16
    for role, lines in turns:
        if role == "user":
            text = lines
            w = (len(text) + 2) * CHAR_W + 22
            body.append(f'<rect x="{PAD_X - 8}" y="{y - 15}" width="{w:.0f}" height="{LINE_H + 2}" '
                        f'rx="5" fill="{COLORS["userbg"]}"/>')
            body.append(f'<text x="{PAD_X}" y="{y}" xml:space="preserve">{tspan("> ", "dim")}'
                        f'{tspan(text, "user", "600")}</text>')
            y += LINE_H + 14
        else:
            for i, segs in enumerate(lines):
                if isinstance(segs, str):
                    segs = [(segs, None)]
                prefix = tspan("● ", "bullet") if i == 0 else tspan("  ")
                spans = "".join(tspan(t, c) for t, c in segs)
                body.append(f'<text x="{PAD_X}" y="{y}" xml:space="preserve">{prefix}{spans}</text>')
                y += LINE_H
            y += 14
    height = y - 2
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{height}" viewBox="0 0 {WIDTH} {height}" role="img" aria-label="{escape(title)}">
<rect x="0.5" y="0.5" width="{WIDTH - 1}" height="{height - 1}" rx="10" fill="{COLORS["bg"]}" stroke="{COLORS["border"]}"/>
<path d="M10.5 0.5 H{WIDTH - 10.5} A10 10 0 0 1 {WIDTH - 0.5} 10.5 V34.5 H0.5 V10.5 A10 10 0 0 1 10.5 0.5 Z" fill="{COLORS["bar"]}"/>
<circle cx="22" cy="18" r="6" fill="#ff5f57"/><circle cx="42" cy="18" r="6" fill="#febc2e"/><circle cx="62" cy="18" r="6" fill="#28c840"/>
<text x="{WIDTH / 2}" y="22.5" text-anchor="middle" font-family="{FONT}" font-size="12.5" fill="{COLORS["title"]}">{escape(title)}</text>
<g font-family="{FONT}" font-size="{SIZE}" fill="{COLORS["text"]}">
{chr(10).join(body)}
</g>
</svg>
'''
    Path(out).write_text(svg, encoding="utf-8")


def paper(title, venue):
    return [("  + ", "plus"), (title.ljust(52), None), (venue, "dim")]


folder = [
    ("user", "organize this folder"),
    ("agent", [
        "Identified most PDFs in your library and OpenAlex, and read the rest",
        "from their own text. Having read them all, I'd group them like this:",
        [("  Local citation recommendation   ", "head"), ("which paper to cite for a sentence", "dim")],
        [("  Global citation recommendation  ", "head"), ("what to cite for a whole paper", "dim")],
        [("  Citation context and intent     ", "head"), ("where and why papers cite", "dim")],
        [("  Datasets and evaluation         ", "head"), ("benchmarks and metrics", "dim")],
        "Shall I sort the PDFs into these folders?",
    ]),
    ("user", "fold citation context into local; the rest is fine"),
    ("agent", ["Done. PDFs sorted into three folders, TOPIC.md written."]),
    ("user", "what papers am I missing?"),
    ("agent", [
        [("From your library", "dim")],
        paper("Content-Based Citation Recommendation", "NAACL 2018"),
        paper("SPECTER: Document-level Representation Learning...", "ACL 2020"),
        [("From OpenAlex", "dim")],
        paper("Context-aware Citation Recommendation", "WWW 2010"),
        [("  − ", "minus"), ("two general surveys, broader than this folder", "dim")],
        "Which ones should I download?",
    ]),
    ("user", "all three"),
    ("agent", [
        "Downloaded the two library papers into their categories and updated",
        "TOPIC.md. The WWW paper is not open access; here is its DOI so you",
        "can get it through your own library.",
    ]),
]

lookup = [
    ("user", "I read a paper that learned paper embeddings from citations, AI2 I think?"),
    ("agent", [
        [("Most likely: ", None), ("SPECTER: Document-level Representation Learning", "head")],
        [("using Citation-informed Transformers", "head"), (" (Cohan et al., ACL 2020)", "dim")],
        "Fairly sure: it trains a transformer so that papers that cite each",
        "other end up close, and the authors are at AI2.",
        [("Close alternative: ", "dim"), ("SciNCL", None), (" (Ostendorff et al., EMNLP 2022)", "dim")],
    ]),
]


def architecture(out):
    """Boxes and arrows in the same dark style as the chats."""
    W, H = 930, 330
    box = {  # name: (x, y, w, h, title, sub lines, dashed)
        "you": (20, 145, 90, 50, "You", [], False),
        "agent": (160, 115, 200, 110, "Coding agent",
                  ["Claude Code or Codex", "follows SKILL.md", "makes every judgement"], False),
        "scripts": (430, 70, 200, 80, "scripts/", ["retrieval, bookkeeping", "no judgement"], False),
        "mcp": (430, 230, 200, 80, "paper-retrieval", ["MCP service", "optional, private"], True),
        "folder": (730, 20, 180, 80, "Topic folder", ["PDFs by category", "TOPIC.md"], False),
        "openalex": (730, 125, 180, 80, "OpenAlex", ["public API", "every venue"], False),
        "archer": (730, 230, 180, 80, "archer", ["private library", "SQLite + PDFs"], False),
    }
    # (x1, y1, x2, y2, label, label_x, label_y, both_ends, dashed)
    edges = [
        (110, 170, 160, 170, "talk", 135, 162, True, False),
        (360, 145, 430, 112, "runs", 395, 120, False, False),
        (360, 200, 430, 262, "MCP tools", 368, 256, False, True),
        (630, 88, 730, 88, "reads, writes", 680, 79, True, False),
        (630, 128, 730, 162, "HTTPS", 688, 133, False, False),
        (615, 150, 730, 250, "SSH", 655, 218, False, False),
        (630, 270, 730, 270, "same library", 680, 261, False, True),
    ]
    parts = []
    for x, y, w, h, title, sub, dashed in box.values():
        dash = ' stroke-dasharray="5 4"' if dashed else ""
        parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" '
                     f'fill="{COLORS["bar"]}" stroke="{COLORS["border"]}" stroke-width="1.5"{dash}/>')
        lines = [(title, "user", 14, "600")] + [(t, "dim", 12.5, None) for t in sub]
        total = 18 + 18 * len(sub)
        ty = y + (h - total) / 2 + 14
        for text, color, size, weight in lines:
            wt = f' font-weight="{weight}"' if weight else ""
            parts.append(f'<text x="{x + w / 2}" y="{ty:.1f}" text-anchor="middle" font-size="{size}" '
                         f'fill="{COLORS[color]}"{wt}>{escape(text)}</text>')
            ty += 18
    for x1, y1, x2, y2, label, lx, ly, both, dashed in edges:
        dash = ' stroke-dasharray="5 4"' if dashed else ""
        start = ' marker-start="url(#arrow)"' if both else ""
        parts.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{COLORS["dim"]}" '
                     f'stroke-width="1.5"{dash}{start} marker-end="url(#arrow)"/>')
        parts.append(f'<text x="{lx}" y="{ly}" text-anchor="middle" font-size="11.5" '
                     f'fill="{COLORS["head"]}">{escape(label)}</text>')
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" aria-label="How paperlevelup is put together">
<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
<path d="M0 1 L9 5 L0 9 Z" fill="{COLORS["dim"]}"/></marker></defs>
<rect x="0.5" y="0.5" width="{W - 1}" height="{H - 1}" rx="10" fill="{COLORS["bg"]}" stroke="{COLORS["border"]}"/>
<g font-family="{FONT}">
{chr(10).join(parts)}
</g>
</svg>
'''
    Path(out).write_text(svg, encoding="utf-8")


out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent
render("~/Papers/CitationRecommendation", folder, out / "chat-organize.svg")
render("any directory", lookup, out / "chat-lookup.svg")
architecture(out / "architecture.svg")
