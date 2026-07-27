---
name: paperlevelup
description: Organize a folder of research paper PDFs into categories, and find
  related papers in the user's own 179k-paper library on the archer host. Use
  when the user wants to organize, classify, sort, triage, or summarize a topic
  folder of papers; when they ask what papers they are missing on a topic, what
  to read next, or which papers are foundational; or when they mention TOPIC.md
  or a Papers/<topic> folder.
---

# Paper Level Up — agent workflow

This file is your operating manual. The user keeps one folder per topic, full of
PDFs named `[year VENUE]title.pdf`. You organize that folder, and you help
expand it from the user's own paper library.

**The division of labour is the whole design.** The scripts do retrieval and
bookkeeping and make no judgements. Every judgement — what a paper is about,
what the categories should be, whether a discovered paper is relevant — is
yours, made in conversation with the user. Do not try to push judgement into
the scripts.

## Running the scripts

They live in `~/.claude/skills/paperlevelup/scripts/`. Call them by absolute
path from wherever you are — no `cd` needed, they find their own config:

```bash
python3 ~/.claude/skills/paperlevelup/scripts/collect.py --topic "<topic folder>"
```

`--topic` is the folder of PDFs, which is normally the directory the user has
open. Every script prints JSON. If the user did not say which folder and the
current directory contains PDFs, use the current directory and say so.

## What exists

The library host (`archer`, reached over SSH; nothing is installed there) holds
~179k papers from 15 top AI/ML venues, 1979–2026, with abstracts for ~96% of
them and 3.8M resolved citation edges between them.

**The library covers only those 15 venues.** A typical topic folder resolves
maybe 40% against it; the rest are workshop papers, journals, SIGIR/KDD/TKDE
style venues, and arXiv preprints. This is expected and fine. It matters in
exactly one place: the citation channel only sees resolved papers, while the
keyword channel works for everything.

| Script | Does |
|---|---|
| `collect.py` | Resolve each PDF against the library, fall back to local text extraction, build the sidecar |
| `organize.py` | Store the taxonomy and your per-paper analysis; move PDFs into category subfolders |
| `report.py` | Write `TOPIC.md` |
| `search.py` | Keyword recall over the whole library |
| `graph.py` | Citation recall: gaps, forward citations, internal structure |
| `fetch.py` | Copy a discovered paper's PDF into the topic folder |

State lives in `<topic>/.paperlevelup/` — `papers.jsonl` (one record per paper)
and `taxonomy.json` (this topic's categories). The PDFs themselves, arranged in
category subfolders, are the visible artefact.

---

## Function 1 — organize the folder (the primary job)

### Step 1: collect

```bash
python3 ~/.claude/skills/paperlevelup/scripts/collect.py --topic "<dir>"
```

This resolves every PDF against the library and writes the sidecar. Papers that
resolve get authoritative metadata; papers that don't get their first pages
extracted locally. Report the match count to the user, and check
`no_readable_text` in the output — anything listed there needs attention before
you try to classify it.

The library is the authority on metadata, the filename is only a hint. Filenames
have been observed to contain typos (`[2925 NAACL]`, `[2025 ENMLP]`) that the
library silently corrects — trust the library. This corrects the *metadata*, not
the file on disk; the filename keeps its typo unless the user asks otherwise.

### Step 2: read the entire topic at once

```bash
python3 ~/.claude/skills/paperlevelup/scripts/collect.py --topic "<dir>" --digest
```

**Read all of it before deciding anything.** Topics here run 8–112 papers, which
fits comfortably in your context. This is the point of the whole design: you get
to see the actual distribution of the collection before naming a single
category.

Do not classify papers one at a time against a taxonomy you guessed up front.
That is what makes classification quality collapse — a per-paper prompt cannot
see the shape of the collection, so the taxonomy becomes a ceiling on the result
rather than a description of it.

### Step 3: propose a taxonomy, then agree on it

Design categories that carve up *this* topic. Generic buckets
(Method / Dataset / Benchmark) are usually the wrong answer: they are the split
you would have guessed without reading anything. Look for the axis the papers
actually differ on — problem formulation, supervision signal, evaluation target,
whatever the collection turns out to be organized around.

Propose it to the user with a one-line rationale per category and a rough count,
and let them push back before you apply anything. Taxonomies here are expected
to be revised; that is not failure.

```bash
python3 ~/.claude/skills/paperlevelup/scripts/organize.py --topic "<dir>" --set-taxonomy '{
  "categories": [
    {"name": "...", "description": "..."},
    {"name": "...", "description": "..."}
  ]
}'
```

### Step 4: assign, which also lays out the folders

```bash
python3 ~/.claude/skills/paperlevelup/scripts/organize.py --topic "<dir>" --assign '[
  {"key": "[2025 EMNLP]SciEvent- ....pdf",
   "category": "...",
   "tags": ["...", "..."],
   "summary": "2-3 sentences",
   "key_contributions": ["...", "..."]}
]'
```

`key` is the PDF filename exactly as it appears on disk. Assigning moves the PDF
into `<topic>/<Category>/`. Use `--dry-run` first on a large batch, `--no-move`
if the user only wants the metadata recorded.

A paper that genuinely spans two categories goes in the one matching its primary
contribution, with the other noted in `tags` or `summary`. If it is a real
coin-flip, ask rather than guess.

### Step 5: report

```bash
python3 ~/.claude/skills/paperlevelup/scripts/report.py --topic "<dir>"
```

Writes `<topic>/TOPIC.md`, grouped by category, with links that open the PDFs.

### When the user rearranges files by hand

The folder is the source of truth. If they drag PDFs between category folders:

```bash
python3 ~/.claude/skills/paperlevelup/scripts/organize.py --topic "<dir>" --adopt-folders
```

This reads the layout back into the sidecar and registers any new category
folder they created. Run it before anything else if you suspect the folder
changed.

---

## Function 2 — find related papers in the library

The user's real process is: pull keywords out of what they already have, search,
read abstracts, decide. It is subjective and it does not want to be automated
into a score. So: **the scripts over-recall, you judge, the user steers.**

### Keyword channel — works for every paper in the folder

```bash
python3 ~/.claude/skills/paperlevelup/scripts/search.py --topic "<dir>" \
  --keywords "citation intent classification,related work generation"
python3 ~/.claude/skills/paperlevelup/scripts/search.py --topic "<dir>" \
  --keywords "..." --year-min 2022 --venue emnlp,acl
```

Derive the keywords from what you read in the digest. Every term in a query must
appear, so multi-word phrases are precise but can come back empty — if that
happens, shorten the phrase rather than reaching for `--match-any`. Run several
rounds with different phrasings; each returns abstracts, so read them and refine.

This channel does not care whether the topic's own papers are in the library,
which is why it is the one that always applies.

### Citation channel — only for papers that resolved

```bash
python3 ~/.claude/skills/paperlevelup/scripts/graph.py --topic "<dir>" --mode gaps
python3 ~/.claude/skills/paperlevelup/scripts/graph.py --topic "<dir>" --mode forward
python3 ~/.claude/skills/paperlevelup/scripts/graph.py --topic "<dir>" --mode internal
```

`gaps` ranks by lift, not raw citation count, and that distinction matters. Raw
count surfaces BERT, Attention Is All You Need, GPT-3 — field infrastructure the
user already knows. Lift divides by corpus-wide popularity, surfacing papers
that are heavily cited *inside this topic* and obscure outside it. Those are the
ones actually worth flagging. Pass `--rank count` only if the user explicitly
wants the classics.

### Then judge, and ask before fetching

Present candidates as a short list with your reasoning, grouped by why they
surfaced. Never dump raw script output at the user. Say what you would keep and
what you would drop.

**Do not fetch anything until the user has said which ones they want.** Pulling
PDFs into their folder changes their filesystem, and the whole point of this
channel is that the final call is theirs.

```bash
python3 ~/.claude/skills/paperlevelup/scripts/fetch.py --topic "<dir>" \
  --ids "<id>,<id>" --category "<existing category>"
```

This copies the PDFs down, names them to the convention, and registers them as
already resolved. Then classify them like any other paper and regenerate the
report.

---

## Principles

- **Read everything before naming anything.** The taxonomy comes out of the
  collection, not out of your priors.
- **The scripts never judge.** If you find yourself wanting a relevance
  threshold in a script, that is a sign the judgement belongs in the
  conversation instead.
- **Say what you did.** Every category you invent, every paper you fetch, every
  file you move — state it and why. Never move files silently.
- **Partial coverage is normal.** Say plainly how many papers resolved, and
  don't let the citation channel's blind spot go unmentioned when you use it.
- **Ask when it is genuinely ambiguous.** A wrong category quietly poisons the
  report; a question costs one turn.
- **Everything is re-runnable.** `collect.py` skips work already done unless
  `--force`; assignment and layout are idempotent. Re-run freely.
