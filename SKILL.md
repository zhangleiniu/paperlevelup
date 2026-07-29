---
name: paperlevelup
description: Organize a folder of research paper PDFs into categories, and find
  related papers via the user's own 179k-paper library on the archer host plus
  OpenAlex. Use when the user wants to organize, classify, sort, triage, or
  summarize a topic folder of papers; when they ask what papers they are missing
  on a topic, what to read next, or which papers are foundational; or when they
  mention TOPIC.md or a Papers/<topic> folder.
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

| Script | Does |
|---|---|
| `collect.py` | Identify each PDF against every configured source, fall back to local text extraction, build the sidecar |
| `organize.py` | Store the taxonomy and your per-paper analysis; move PDFs into category subfolders |
| `report.py` | Write `TOPIC.md` |
| `search.py` | Keyword recall across sources |
| `graph.py` | Citation recall: gaps, forward citations, internal structure |
| `fetch.py` | Copy a discovered paper's PDF into the topic folder |

State lives in `<topic>/.paperlevelup/` — `papers.jsonl` (one record per paper),
`taxonomy.json` (this topic's categories), and `cache/` (raw API responses, so
re-runs stay off the network). The PDFs themselves, arranged in category
subfolders, are the visible artefact.

## The two sources

They are **not interchangeable** — each is authoritative about different things,
and every script takes `--sources` to pick between them (default: both, archer
first).

|  | **archer** | **OpenAlex** |
|---|---|---|
| Reached by | SSH, nothing installed there | public HTTP API, no key |
| Coverage | 179k papers, **15 AI/ML venues only**, 1979–2026 | ~250M works, every venue |
| PDFs | **all of them** | open-access only (~43% of matches) |
| Abstracts | ~96% | ~76% |
| Citation edges | 3.8M, but only *between corpus papers* | global, but **~48% of works** |
| DOI / global citation count | no | yes |

Two consequences you must keep straight:

- **archer's blind spot is the venue.** SIGIR, KDD, TKDE, ECIR, TPDL,
  Scientometrics and most journals are simply absent. Searching it for
  "local citation recommendation" returns almost nothing — not because the work
  does not exist but because it was published where archer does not look.
- **OpenAlex's blind spot is the reference list.** Coverage depends on
  publisher and Crossref deposits, so a preprint-heavy folder can have a much
  thinner graph. The gap is *systematic*, not random. `graph.py` prints the
  exact ratio; quote it rather than presenting a thin graph as a complete one.

Measured on a real 49-paper folder: archer alone identified 24, adding OpenAlex
took it to 49.

---

## Function 1 — organize the folder (the primary job)

### Step 1: collect

```bash
python3 ~/.claude/skills/paperlevelup/scripts/collect.py --topic "<dir>"
```

Each configured source is queried independently and the metadata is merged
field by field — so a paper can take its title from archer and its DOI from
OpenAlex. On later runs, only newly configured or previously untried sources
trigger enrichment. Each record keeps a `provenance` map saying which source
gave which field, because with two backends "the library is the authority" is
no longer one sentence you can put in the docs.

Report `resolved_by_source` to the user, and check `no_readable_text` — anything
listed there needs attention before you try to classify it.

Adding a source to the config and running collect normally enriches records that
have not tried it yet. `--retry-unresolved` explicitly re-attempts papers that
no source identified, while `--force` re-runs every paper.

**A resolved record beats the filename.** Filenames have been observed to
contain typos (`[2925 NAACL]`, `[2025 ENMLP]`) that the sources silently
correct — trust the source. This corrects the *metadata*, not the file on disk;
the filename keeps its typo unless the user asks otherwise.

When nothing matches, read `near_miss` before concluding the paper is absent.
Truncated filenames are the usual cause: `Experiments with Relevance and
Rhetorical Status` is really `Summarizing Scientific Articles: Experiments with
Relevance and Rhetorical Status`, which scores only 0.75 on similarity. The
matcher accepts verbatim containment of 30+ characters for exactly this case,
but a shorter truncation still needs your eye.

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

## Function 2 — find related papers

The user's real process is: pull keywords out of what they already have, search,
read abstracts, decide. It is subjective and it does not want to be automated
into a score. So: **the scripts over-recall, you judge, the user steers.**

### Keyword channel — always applies

```bash
python3 ~/.claude/skills/paperlevelup/scripts/search.py --topic "<dir>" \
  --keywords "citation intent classification,related work generation"
python3 ~/.claude/skills/paperlevelup/scripts/search.py --topic "<dir>" \
  --keywords "..." --year-min 2022 --venue emnlp,acl
python3 ~/.claude/skills/paperlevelup/scripts/search.py --topic "<dir>" \
  --keywords "..." --sources openalex        # when the topic lives outside archer's venues
```

Derive the keywords from what you read in the digest. Run several rounds with
different phrasings; each returns abstracts, so read them and refine.

The two backends behave differently and it shows. archer requires every term of
a query to appear — precise, but it can come back empty, in which case shorten
the phrase rather than reaching for `--match-any`. OpenAlex searches titles and
abstracts by default and its tail can still be noisy; `--fulltext` broadens it
further and should be reserved for a scoped query that is too thin.

`rank_in_source` is each backend's own ordering and is **not comparable across
sources** — the output says so, and you should not treat a rank-1 from one as
beating a rank-5 from the other. A paper found by *both* is a real signal, and
`found_by` records it.

### Citation channel

```bash
python3 ~/.claude/skills/paperlevelup/scripts/graph.py --topic "<dir>" --mode gaps
python3 ~/.claude/skills/paperlevelup/scripts/graph.py --topic "<dir>" --mode forward
python3 ~/.claude/skills/paperlevelup/scripts/graph.py --topic "<dir>" --mode internal
```

`gaps` ranks by lift, not raw citation count, and that distinction matters. Raw
count surfaces BERT, Attention Is All You Need, GPT-3 — field infrastructure the
user already knows. Lift divides by popularity, surfacing papers that are
heavily cited *inside this topic* and obscure outside it. Those are the ones
actually worth flagging. Pass `--rank count` only if the user explicitly wants
the classics.

**Results come back in one block per source, and that is deliberate.** Lift
needs a popularity denominator and the two do not share one: archer can only
count citations inside its own corpus, OpenAlex knows the global count. Merging
them into a single ranked list would produce numbers that look comparable and
are not. Read both blocks; they genuinely surface different papers, because the
communities they cover barely overlap.

### Then judge, and ask before fetching

Present candidates as a short list with your reasoning, grouped by why they
surfaced. Never dump raw script output at the user. Say what you would keep and
what you would drop.

**Do not fetch anything until the user has said which ones they want.** Pulling
PDFs into their folder changes their filesystem, and the whole point of this
channel is that the final call is theirs.

```bash
python3 ~/.claude/skills/paperlevelup/scripts/fetch.py --topic "<dir>" \
  --ids "openalex:W123,archer:<uuid>" --category "<existing category>"
```

Ids are namespaced; a bare id means archer. This copies the PDFs down, names
them to the convention, and registers them as already resolved. Then classify
them like any other paper and regenerate the report.

**Not everything can be fetched, and that is not a bug.** archer serves any PDF
it has. OpenAlex only yields a file when the work is open access *and* carries a
recognised licence — we do not follow landing pages, retry past paywalls, or
guess at unlabelled licences. A paywalled paper comes back as `no-pdf` with its
DOI. Pass that DOI on to the user so they can use their own institutional
access; do not go looking for the PDF elsewhere.

---

## Principles

- **Read everything before naming anything.** The taxonomy comes out of the
  collection, not out of your priors.
- **The scripts never judge.** If you find yourself wanting a relevance
  threshold in a script, that is a sign the judgement belongs in the
  conversation instead.
- **Say what you did.** Every category you invent, every paper you fetch, every
  file you move — state it and why. Never move files silently.
- **Partial coverage is normal, but name which coverage.** Say plainly how many
  papers resolved and via which source. The two blind spots are different:
  archer misses whole *venues*, OpenAlex misses *reference lists* for preprints.
  When you use the citation channel, quote the ratio the script prints rather
  than letting a half-covered graph pass as a complete one.
- **Never fuse rankings across sources.** Dedup by DOI or normalised title is
  bookkeeping and belongs in a script; deciding that one backend's third result
  beats another's first is judgement and belongs in the conversation.
- **Ask when it is genuinely ambiguous.** A wrong category quietly poisons the
  report; a question costs one turn.
- **Everything is re-runnable.** `collect.py` skips work already done unless
  `--force`; assignment and layout are idempotent. Re-run freely.
