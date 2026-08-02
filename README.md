# Paper Level Up

Organize a folder of papers with a coding agent (Claude Code / Codex), backed by
your own paper library.

You keep one folder per topic, full of PDFs named `[year VENUE]title.pdf`. Open
it with Claude Code or Codex, ask the agent to organize it, and it reads the
whole collection, proposes a taxonomy, sorts the PDFs into category subfolders,
and writes a `TOPIC.md`. It can then search your library for papers you're
missing.

> **Status: personal setup, published as a working reference.**
>
> One of the two sources is *my* paper library — a private host (`archer`)
> running a SQLite corpus of ~179k papers, a Meilisearch index over it, and a
> PDF store. That library is not part of this repo and is not something you can
> stand up from it.
>
> The other source is **OpenAlex**, which is public and needs no key. Setting
> `"sources": ["openalex"]` in the config gives you identification, keyword
> discovery and a citation graph without any private infrastructure — that path
> does run end-to-end on your machine. You lose the curated PDF store and the
> dense in-corpus edges.
>
> What is reusable regardless is everything above the corpus: the agent workflow
> in [`SKILL.md`](SKILL.md), the division of labour between agent and scripts,
> and the decisions in [Design notes](#design-notes).

This checkout is the canonical copy at `~/.claude/skills/paperlevelup/`.
Claude Code loads it there, while Codex loads the same directory through the
symlink `~/.agents/skills/paperlevelup`. Keeping one real copy means edits to
`SKILL.md`, scripts, tests, and config are immediately shared by both agents.

The `name`/`description` frontmatter at the top of `SKILL.md` controls when the
skill is considered and loaded. Keep that block intact when editing.

## How it's put together

The agent is the orchestrator, not a fixed pipeline. `scripts/` exposes
capabilities; `SKILL.md` tells the agent how to use them; every judgement — what
the categories should be, whether a discovered paper is relevant — happens in
conversation, where you can redirect it. Nothing is hardcoded as a prompt.

Retrieval and bookkeeping run in `scripts/` through a small source interface.
The private archer adapter ships short programs over SSH stdin, while the
OpenAlex adapter calls its public HTTP API directly. Nothing is installed on
the library host, and its Meilisearch key never leaves that machine.

## Setup

```bash
pip install pypdf
```

For Codex, expose this same checkout through its user-level skills directory:

```bash
mkdir -p ~/.agents/skills
ln -s ~/.claude/skills/paperlevelup ~/.agents/skills/paperlevelup
```

Verify the link with `ls -l ~/.agents/skills/paperlevelup`, then restart Codex
if it was already running. Codex supports symlinked skill directories, so do
not copy the project into both locations: keep editing the canonical checkout
under `~/.claude/skills/paperlevelup/`.

You only need SSH access when using the private archer source. For an
OpenAlex-only setup, set `"sources": ["openalex"]`. The archer values tracked in
`config/remote.json` are mine and will not resolve on your machine — they
document the shape rather than providing a working public default:

| Key | Meaning |
|---|---|
| `host` | SSH alias of the library host |
| `db_path` | SQLite paper database on that host |
| `pdf_root` | Where its PDFs live |
| `meili_url` / `meili_index` | Meilisearch endpoint, as seen from that host |
| `match_threshold` | Title similarity needed to call it the same paper (0.85) |

Put machine-specific overrides in `config/remote.local.json` (gitignored) rather
than editing the tracked file.

The archer adapter opens one SSH connection and reuses it, so an archer run is
one handshake, not one per paper.

## Use it

Open the topic folder with Claude Code or Codex and say:

> organize this folder

It will collect, read everything, propose a taxonomy, wait for you to agree,
then sort the PDFs and write the report. Later:

> what papers am I missing on this topic?

Adding more PDFs and repeating the first instruction is safe — already-analysed
papers are skipped. In Claude Code, `/paperlevelup` invokes the skill explicitly.
In Codex CLI or the IDE extension, use `/skills` to confirm discovery and invoke
it with:

```text
$paperlevelup organize this folder
```

Natural-language requests can also trigger it implicitly when they match the
description in `SKILL.md`.

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
python3 $PLU/search.py --topic "$TOPIC" --keywords "..." --sources openalex
python3 $PLU/graph.py  --topic "$TOPIC" --mode gaps
python3 $PLU/fetch.py  --topic "$TOPIC" --ids "openalex:W123,archer:<uuid>" --category X

# explicitly retry papers that no source identified
python3 $PLU/collect.py --topic "$TOPIC" --retry-unresolved

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
    ├── taxonomy.json           this topic's categories
    └── cache/                  raw API responses, per source
```

The folder is the source of truth. Drag a PDF into a different category folder
and `organize.py --adopt-folders` picks the change up. Records are keyed by
filename, which survives moves between categories.

Identity is a dict — `ids: {archer, openalex, doi}` — because no single id is
*the* identity once there is more than one backend: archer knows where the PDF
is, OpenAlex knows the DOI and the global citation count. DOI is the join key
between them. Each record also carries `provenance`, mapping every field to the
source it came from.

## Two sources

`config/remote.json` lists which sources to use and in what order; every script
takes `--sources` to override it for one run.

During collection, configured sources are queried independently so their fields
can be merged. Later runs only trigger enrichment for newly configured or
previously untried sources unless `--force` is passed.

|  | **archer** (private) | **OpenAlex** (public) |
|---|---|---|
| Coverage | 179k papers, 15 AI/ML venues | ~250M works, every venue |
| PDFs | all | open access only (~43% of matches) |
| Abstracts | ~96% | ~76% |
| Citation edges | 3.8M, between corpus papers only | global, but present for ~48% of works |
| DOI, global citation count | — | yes |

The blind spots are different in kind, which is the whole reason for having
both. archer misses whole **venues** — SIGIR, KDD, TKDE, ECIR, Scientometrics
and most journals are absent, so searching it for "local citation
recommendation" returns almost nothing. OpenAlex misses **reference lists**:
coverage depends on publisher and Crossref deposits, so a preprint-heavy folder
can have a much thinner citation graph.

On a real 49-paper folder, archer alone identified 24; adding OpenAlex took it
to 49.

Set `openalex.mailto` in `config/remote.local.json` to join OpenAlex's polite
pool (10 req/s, 100k/day). Responses are cached under
`<topic>/.paperlevelup/cache/`, so a re-run costs nothing.

## Two channels for discovery

**Keyword** (`search.py`) searches every configured source. It works for every
paper in your folder, because the keywords come from reading the papers, not
from the papers being indexed.

**Citation** (`graph.py`) walks the citation graph. Its `gaps` mode ranks by
lift rather than raw citation count: raw count just returns the field's famous
papers, while lift finds papers heavily cited inside your topic and obscure
outside it.

Results are reported **one block per source, never merged into a single
ranking**. Lift needs a popularity denominator and the two do not share one:
archer can only count citations inside its own corpus, OpenAlex knows the global
count — which is what lift was always reaching for. Merging them would produce
numbers that look comparable and are not.

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

A third decision came with the second source:

- **Field-level merge, not a source hierarchy.** The obvious design is "archer
  wins, OpenAlex fills gaps", but that is wrong: they are authoritative about
  different things, not the same things with different reliability. So metadata
  merges field by field against a fixed precedence table and each record records
  where every field came from. The same reasoning is why `graph.py` reports one
  block per source instead of one merged ranking — deduplicating by DOI is
  bookkeeping, but deciding whose rank-3 beats whose rank-1 is judgement, and
  judgement does not go in the scripts.

### Choosing a source, if you are adapting this

Semantic Scholar was the obvious third candidate — it parses references out of
PDFs, so it has the arXiv reference lists OpenAlex lacks, plus TLDRs and
citation intents. It was dropped for now: the unauthenticated pool returns HTTP
429 on essentially every request (five calls at 3.5s spacing, all rejected), so
it is not usable without an API key. Worth adding behind a key if you have one.
