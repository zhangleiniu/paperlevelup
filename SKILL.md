---
name: paperlevelup
description: Organize a folder of research paper PDFs into categories, and find
  papers via the user's own ~185k-paper AI/ML library (searched through the
  paper-retrieval MCP tools when they are connected, otherwise directly on the
  library host over SSH) plus OpenAlex for every other venue. Use when the user wants to
  organize, classify, sort, triage, or summarize a topic folder of papers; when
  they ask what papers they are missing on a topic, what to read next, or which
  papers are foundational; when they ask for papers on a topic, related work for
  a paper, or baselines to compare with, with or without a folder; when they
  update a curated paper list; or when they mention TOPIC.md or a Papers topic
  folder.
---

# Paper Level Up — agent workflow

This file is your operating manual. The user keeps one folder per topic, full of
PDFs named `[year VENUE]title.pdf`. You organize that folder, and you help
expand it from the user's own paper library and OpenAlex. The same retrieval
also answers paper questions that involve no folder (see "Without a folder").

**The division of labour is the whole design.** The scripts do retrieval and
bookkeeping and make no judgements. Every judgement — what a paper is about,
what the categories should be, whether a discovered paper is relevant — is
yours, made in conversation with the user. Do not try to push judgement into
the scripts.

## Running the scripts

The canonical checkout normally lives at
`~/.claude/skills/paperlevelup/`; Codex may discover the same directory through
`~/.agents/skills/paperlevelup`. The latter should be a symlink, not a second
copy, so both agents always run the same scripts.

Call the scripts by absolute path from wherever you are — no `cd` needed, they
find their own config:

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
| `metadata.py` | Create, validate, remove, and apply evidence-bearing metadata overrides when retrieval sources fail |
| `organize.py` | Store the taxonomy and your per-paper analysis; move PDFs into category subfolders |
| `report.py` | Write `TOPIC.md` |
| `search.py` | Keyword recall across sources (OpenAlex; archer as cross-check and fallback) |
| `graph.py` | Citation recall: gaps, forward citations, internal structure |
| `fetch.py` | Copy a discovered paper's PDF into the topic folder |

State lives in `<topic>/.paperlevelup/` — `papers.jsonl` (one record per paper),
`taxonomy.json` (this topic's categories), and `cache/` (raw API responses, so
re-runs stay off the network). The PDFs themselves, arranged in category
subfolders, are the visible artefact.

## The sources

The library is reached two ways. When the **paper-retrieval MCP tools**
(`find_papers`, `find_related_papers`, `get_paper`, `search_passages`,
`exact_search`, `get_citation_neighbors`, `get_research_profile`) are available,
they are the search channel for the library: full-text passages, BM25 and
embeddings fused and reranked, plus citation-graph expansion, with an evidence
passage for every result. They return the same paper ids as archer, so a
result's `paper_id` is directly usable as `archer:<paper_id>` by `fetch.py` and
`graph.py`. When the library database is built from the same pipeline run as
the service (the `snapshot` field names it), the ids always agree. If they
differ, a paper whose title, venue or year changed between the runs may carry a
different id, and `fetch.py` will not find it. Then look the paper up by title
with `search.py --sources archer` and fetch the id that returns. They cover only the library, never OpenAlex. The scripts below use
archer directly for identification, PDFs and citation edges, and keep archer
keyword search as a cross-check and as the fallback when the tools are absent
or return an error.

archer and OpenAlex are **not interchangeable** — each is authoritative about
different things, and every script takes `--sources` to pick between them
(default: both, archer first).

|  | **archer** | **OpenAlex** |
|---|---|---|
| Reached by | SSH, nothing installed there | public HTTP API, no key |
| Coverage | ~185k papers, **15 AI/ML venues only**, 1979–2026 | ~250M works, every venue |
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

#### Correct metadata only after retrieval fails

When all configured sources fail or omit a required bibliographic field, verify
the value against the PDF title page or another primary source, then record it
through `metadata.py`:

```bash
python3 ~/.claude/skills/paperlevelup/scripts/metadata.py --topic "<dir>" --set '[
  {"key":"[2026 VENUE]Paper.pdf", "authors":["A. Author"],
   "venue":"Venue Name", "evidence":"PDF title page"}
]'
python3 ~/.claude/skills/paperlevelup/scripts/metadata.py --topic "<dir>" --check
```

The exact PDF basename is the key. Supported fields are title, authors, venue,
year, DOI, and URL. Every record requires an evidence note. The script writes
`.paperlevelup/metadata-overrides.json`, applies the fields to `papers.jsonl`,
and marks per-field provenance as `metadata_override`. `collect.py` reapplies
the ledger after every future source refresh. Never edit either file directly;
use `--remove <PDF_BASENAME>` to withdraw a correction.

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
into a score. So: **the tools over-recall, you judge, the user steers.**

Run the channels below and present what they return in **separate blocks, one
per channel** (library, OpenAlex, citation graph). Each ranks by its own
scoring; do not merge them into one ranked list.

### Library channel — paper-retrieval tools, when available

Derive concrete queries from what you read in the digest: the topic's terms,
its sub-problems, the names its papers use. Call `find_papers` once per query.

```
collect.py --topic "<dir>" --known-ids      -> exclude_ids for the papers already in the folder
find_papers(query="<one concrete query>", original_query="<the same query>",
            k=40, filters={"exclude_ids": [...], "year_min": ...})
```

- **Several concrete queries, not one abstract one.** For a folder or a curated
  list, each query is its own question: pass it as both `query` and
  `original_query`, k 40–100. A paragraph describing the topic as the query
  recalls less than a handful of specific phrases (measured on one list: 25 vs
  30 of 38 known papers).
- **`find_related_papers` for a few seeds only** — the folder's central papers,
  or the one paper the user asked about (`paper_id`, `url`, or `text` = title +
  abstract for a paper outside the library). Run on every paper in a folder, its
  citation-graph channel mostly returns field-wide classics (BERT, Adam, RAG).
  Add `relation_type="baseline"` when the user wants baselines.
- **Judge from the `evidence` passage** each result carries, and cite the
  result's `url`, never its internal id. A result found only by the `graph`
  channel (see `ranks`) is a citation neighbour of the top hits, often a
  well-known paper the user has.
- The first call after a quiet period can take ~20 s while the service reloads.
  If the tools are missing or return an error, say so and use the archer keyword
  search below instead.

### Keyword channel — OpenAlex always, archer as cross-check

```bash
python3 ~/.claude/skills/paperlevelup/scripts/search.py --topic "<dir>" \
  --keywords "..." --sources openalex        # every venue outside the library
python3 ~/.claude/skills/paperlevelup/scripts/search.py --topic "<dir>" \
  --keywords "citation intent classification,related work generation" \
  --sources archer --per-query 40            # exact-term cross-check of the library
python3 ~/.claude/skills/paperlevelup/scripts/search.py --topic "<dir>" \
  --keywords "..." --year-min 2022 --venue emnlp,acl
```

OpenAlex is the only channel for SIGIR, KDD, WWW, CIKM, journals and arXiv;
run it whenever the topic reaches past the 15 library venues. Run several rounds
with different phrasings; each returns abstracts, so read them and refine.

archer keyword search is still worth one pass with the same keywords even when
the paper-retrieval tools work: it is cheap, and a paper whose title contains
the query verbatim can rank below k after reranking (on one list it added 5 of
38 known papers the tools missed). It is not a substitute: it requires every
term of a query to appear, so papers worded differently from the query never
surface, and its matches are often coincidental. If a strict query comes back
empty, shorten the phrase rather than reaching for `--match-any`. OpenAlex
searches titles and abstracts by default and its tail can still be noisy;
`--fulltext` broadens it further and should be reserved for a scoped query that
is too thin.

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
surfaced, in the conversation. Never dump raw script output at the user, and do
not write candidate lists or other intermediate results to files or new
directories unless the user asks for one. Say what you would keep and what you
would drop.

**Do not fetch anything until the user has said which ones they want.** Pulling
PDFs into their folder changes their filesystem, and the whole point of this
channel is that the final call is theirs.

```bash
python3 ~/.claude/skills/paperlevelup/scripts/fetch.py --topic "<dir>" \
  --ids "openalex:W123,archer:<uuid>" --category "<existing category>"
```

Ids are namespaced; a bare id means archer. A paper-retrieval result is fetched
as `archer:<paper_id>`. This copies the PDFs down, names them to the
convention, and registers them as already resolved. Then classify them like any
other paper and regenerate the report.

**Not everything can be fetched, and that is not a bug.** archer serves any PDF
it has. OpenAlex only yields a file when the work is open access *and* carries a
recognised licence — we do not follow landing pages, retry past paywalls, or
guess at unlabelled licences. A paywalled paper comes back as `no-pdf` with its
DOI. If the search result listed `other_ids` (OpenAlex often holds a preprint
and its proceedings version as separate works under one title), try those ids
before giving up — one of them may be open access. Otherwise pass the DOI on to
the user so they can use their own institutional access; do not go looking for
the PDF elsewhere.

### Without a folder

Not every request comes with a topic folder: "papers on X", "work related to
this paper", "baselines for Y", "where does a paper discuss Z", or new entries
for a curated paper list (an awesome list, a related-work section).

- **A specific paper the user half-remembers** ("the paper that showed…", "I read
  a paper where…", "which paper proposed…"): they want one paper identified, not
  a list. Call `find_papers` with k=10, the question verbatim as
  `original_query`, and as `query` your guess at its title or its distinctive
  details (method name, dataset, result, author), in English. A remembered venue
  or year may be wrong: do not filter on it on the first call — use it to rank
  the candidates, or filter with a year range of ±1. If the paper is not clearly
  among the results, run `exact_search` on a distinctive term or the guessed title
  (here it finds papers, not only checks them); if the evidence for the best
  match is thin, confirm it with `get_paper`. Give that paper first, as the
  answer, say how sure you are and which remembered details its evidence confirms
  and which it does not, then at most 2 close alternatives; do not list the rest
  (this overrides "report every result" below, which still holds for every other
  request). If nothing fits, run `search.py --keywords "..." --sources openalex`
  — the paper may be outside the library's venues — before saying it was not
  found.
- **A single question** ("which papers do X?"): call `find_papers` with the
  user's question verbatim as both `query` and `original_query`, k=20 unless
  they ask for more. Report every result in the order returned, each with title,
  venue, year, `url` and the line of its evidence that answers the question, and
  mark the strongest. Rephrase and search again only if the results clearly miss
  the question, keeping `original_query` verbatim; for more coverage raise `k`
  rather than splitting the question into facets. Pass already-shown papers in
  `filters.exclude_ids` on follow-ups.
- **Papers by an author** ("X's papers on Y"): put Y in `query` and X's full name
  in `filters.authors` (also on `find_related_papers`) rather than recalling X's
  papers yourself; initials do not match and namesakes are not told apart. If the
  response's `notes` say the name has no papers, retry with a similar name it
  suggests.
- **A paper**: `find_related_papers` (`relation_type="baseline"` for baselines).
- **A claim about a specific paper** (does it use dataset X, what is its method,
  who cites it): `search_passages`, `get_paper(section=...)`, `exact_search`,
  `get_citation_neighbors`, `get_research_profile`. These check candidates; they
  are not for finding more papers.
- **A curated list**: treat it like a folder — several concrete queries, a few
  seeds, the archer cross-check — and drop what the list already holds by DOI,
  URL, openreview id and normalized title, including list titles that contain a
  candidate's title.
- **Outside the library's venues**: `search.py --keywords "..." --sources
  openalex` without `--topic`.

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
- **Never fuse rankings across sources** — paper-retrieval, archer keyword
  search, OpenAlex and the citation graph each rank by their own scoring.
  Dedup by DOI or normalised title is bookkeeping and belongs in a script;
  deciding that one backend's third result beats another's first is judgement
  and belongs in the conversation.
- **Ask when it is genuinely ambiguous.** A wrong category quietly poisons the
  report; a question costs one turn.
- **Everything is re-runnable.** `collect.py` skips work already done unless
  `--force`; assignment and layout are idempotent. Re-run freely.
