# Paper Level Up

Organize a folder of papers with a coding agent (Claude Code / Codex), backed by
your own paper library.

You keep one folder per topic, full of PDFs named `[year VENUE]title.pdf`. Open
it in VS Code, ask Claude Code to organize it, and it reads the whole collection,
proposes a taxonomy, sorts the PDFs into category subfolders, and writes a
`TOPIC.md`. It can then search your library for papers you're missing.

> **Status: personal setup, published as a working reference.**
>
> The scripts talk to *my* paper library — a private host (`archer`) running a
> SQLite corpus of ~179k papers, a Meilisearch index over it, and a PDF store.
> That library is not part of this repo and is not something you can stand up
> from it. Without it `collect.py` fails at the SSH call rather than falling
> back to local extraction, so **this will not run end-to-end on your machine.**
>
> What is reusable is everything above the corpus: the agent workflow in
> [`SKILL.md`](SKILL.md), the division of labour between agent and scripts, and
> the two design decisions in [Design notes](#design-notes). Point the config at
> your own index and the six scripts are maybe an afternoon of adaptation.

This project is installed as a Claude Code skill at
`~/.claude/skills/paperlevelup/`, which is why Claude Code finds it from any
directory. The `name`/`description` frontmatter at the top of `SKILL.md` is what
makes that work — Claude Code reads every skill's frontmatter at startup and
loads the body only when the description matches what you're asking for. Keep
that block intact when editing.

## How it's put together

The agent is the orchestrator, not a fixed pipeline. `scripts/` exposes
capabilities; `SKILL.md` tells the agent how to use them; every judgement — what
the categories should be, whether a discovered paper is relevant — happens in
conversation, where you can redirect it. Nothing is hardcoded as a prompt.

Retrieval and bookkeeping run in `scripts/`, which are thin SSH wrappers around
the library host. Nothing is installed on the library host: each script ships a
short program over SSH stdin. The Meilisearch key is read from the library
host's own `.env` at run time and never leaves it.

## Setup

```bash
pip install pypdf
```

You need SSH access to the library host with keys configured. The values tracked
in `config/remote.json` are mine and will not resolve on your machine — they are
there to document the shape, not as a default that works:

| Key | Meaning |
|---|---|
| `host` | SSH alias of the library host |
| `db_path` | SQLite paper database on that host |
| `pdf_root` | Where its PDFs live |
| `meili_url` / `meili_index` | Meilisearch endpoint, as seen from that host |
| `match_threshold` | Title similarity needed to call it the same paper (0.85) |

Put machine-specific overrides in `config/remote.local.json` (gitignored) rather
than editing the tracked file.

The scripts open one SSH connection and reuse it, so a run is one handshake, not
one per paper.

## Use it

Open the topic folder in VS Code and say:

> organize this folder

It will collect, read everything, propose a taxonomy, wait for you to agree,
then sort the PDFs and write the report. Later:

> what papers am I missing on this topic?

Adding more PDFs and repeating the first instruction is safe — already-analysed
papers are skipped. `/paperlevelup` invokes the skill explicitly if it doesn't
trigger on its own.

Codex does not read Claude Code skills. To use it there, drop an `AGENTS.md` in
the topic folder pointing at `~/.claude/skills/paperlevelup/SKILL.md`.

## Running the scripts yourself

The scripts find their own config, so call them by absolute path from anywhere —
no `cd` required.

```bash
PLU=~/.claude/skills/paperlevelup/scripts
TOPIC=~/Downloads/Papers/MyTopic

# resolve every PDF against the library, extract text for the rest
python3 $PLU/collect.py --topic "$TOPIC"

# print every abstract in one pass (this is what the agent reads)
python3 $PLU/collect.py --topic "$TOPIC" --digest

# taxonomy and classification; assigning also moves the PDFs
python3 $PLU/organize.py --topic "$TOPIC" --set-taxonomy '{"categories":[{"name":"X","description":"..."}]}'
python3 $PLU/organize.py --topic "$TOPIC" --assign '[{"key":"file.pdf","category":"X","summary":"..."}]'
python3 $PLU/organize.py --topic "$TOPIC" --adopt-folders   # you moved files by hand
python3 $PLU/organize.py --topic "$TOPIC" --sync            # re-apply the layout

# discovery
python3 $PLU/search.py --topic "$TOPIC" --keywords "term one,term two" --year-min 2022
python3 $PLU/graph.py  --topic "$TOPIC" --mode gaps
python3 $PLU/fetch.py  --topic "$TOPIC" --ids "<id>,<id>" --category X

# readable output
python3 $PLU/report.py --topic "$TOPIC"
```

## What lands in the topic folder

```
MyTopic/
├── TOPIC.md                    generated report, links open the PDFs
├── <Category>/                 PDFs, physically sorted
│   └── [2025 EMNLP]Title.pdf
└── .paperlevelup/
    ├── papers.jsonl            one record per paper
    └── taxonomy.json           this topic's categories
```

The folder is the source of truth. Drag a PDF into a different category folder
and `organize.py --adopt-folders` picks the change up. Records are keyed by
filename, which survives moves between categories; when a paper resolves against
the library it also carries that library's global id, so the same paper in three
topic folders is three PDFs but one identity.

## Two channels for discovery

**Keyword** (`search.py`) searches title, abstract, authors and venue across the
whole library. It works for every paper in your folder, because the keywords
come from reading the papers, not from the papers being indexed.

**Citation** (`graph.py`) uses the library's resolved citation graph, so it only
sees papers that are in the library — typically around 40% of a topic folder.
Its `gaps` mode ranks by lift rather than raw citation count: raw count just
returns the field's famous papers, while lift finds papers that are heavily
cited inside your topic and obscure outside it. Those are the ones you're
actually likely to have missed.

Both channels over-recall on purpose and return abstracts. Nothing scores
relevance for you — the agent reads them and you correct it.

## Design notes

Two earlier ideas were tried and dropped:

- **A per-paper pipeline with fixed prompts and a fixed schema.** That
  architecture solves a scale problem these topics don't have (the largest is
  112 papers, which fits in one context). Worse, classifying one paper at a time
  means never seeing the collection's shape, so the hand-written taxonomy
  becomes a ceiling on quality. Reading everything at once and deriving the
  taxonomy from the actual distribution is strictly better at this scale.

- **Quantifying relevance for discovery.** Finding related work is subjective:
  you pull out keywords, search, skim abstracts, and decide. Encoding that as a
  score just freezes one person's judgement into a constant. The scripts recall
  widely and the judgement stays in the conversation, where it can be argued
  with.
