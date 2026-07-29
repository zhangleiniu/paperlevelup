"""Shared helpers: config, remote execution on the library host, filename parsing, sidecar I/O.

Design notes
------------
Every heavy operation runs *on the library host* (archer). We ship a short Python
program over SSH stdin instead of deploying code there, so there is nothing to
install or keep in sync on the remote side. The Meilisearch API key is read from
the library host's own .env at run time and never travels to this machine.

The local topic folder is the source of truth for *which papers are in scope*.
Everything the agent computes lives beside it in `<topic>/.paperlevelup/`.
"""
import json
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config" / "remote.json"

SIDECAR_DIR = ".paperlevelup"
PAPERS_FILE = "papers.jsonl"
TAXONOMY_FILE = "taxonomy.json"


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

def load_config() -> dict:
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    local = CONFIG_PATH.with_name("remote.local.json")
    if local.exists():
        cfg.update(json.loads(local.read_text(encoding="utf-8")))
    return cfg


# --------------------------------------------------------------------------
# remote execution
# --------------------------------------------------------------------------

# Available to every remote body: db(), meili_search(), hydrate(), norm(),
# ratio(), emit(), plus CONFIG and PAYLOAD.
REMOTE_PRELUDE = r'''
import difflib, json, os, sqlite3, sys, urllib.request

_con = None
def db():
    global _con
    if _con is None:
        _con = sqlite3.connect(CONFIG["db_path"])
        _con.row_factory = sqlite3.Row
    return _con

_key = None
def meili_key():
    global _key
    if _key is None:
        _key = os.environ.get("MEILI_KEY", "")
        env_path = os.path.join(CONFIG["mustcite_root"], ".env")
        if not _key and os.path.exists(env_path):
            with open(env_path, encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("MEILI_KEY="):
                        _key = line.split("=", 1)[1].strip()
                        break
    if not _key:
        raise RuntimeError("MEILI_KEY not found on the library host")
    return _key

def meili_search(query, limit=20, matching_strategy=None):
    payload = {"q": query, "limit": limit}
    if matching_strategy:
        # "all" makes every term mandatory. The default drops trailing terms,
        # which turns a phrase query into an OR over its commonest word.
        payload["matchingStrategy"] = matching_strategy
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        CONFIG["meili_url"] + "/indexes/" + CONFIG["meili_index"] + "/search",
        data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + meili_key()})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read()).get("hits", [])

def hydrate(ids):
    """Authoritative metadata straight from SQLite. Meilisearch documents have
    coerced types (year is a string, authors is flattened), so never trust them
    for anything but recall."""
    out = {}
    ids = [i for i in ids if i]
    for start in range(0, len(ids), 400):
        chunk = ids[start:start + 400]
        placeholders = ",".join("?" * len(chunk))
        rows = db().execute(
            "select id, title, abstract, authors, venue, year, pdf_path, url "
            "from papers where id in (%s)" % placeholders, chunk)
        for row in rows:
            rec = dict(row)
            raw = rec.get("authors")
            try:
                rec["authors"] = json.loads(raw) if raw else []
            except (ValueError, TypeError):
                rec["authors"] = [raw] if raw else []
            out[rec["id"]] = rec
    return out

def norm(text):
    cleaned = "".join(
        c if (c.isalnum() or c.isspace()) else " " for c in (text or "").lower())
    return " ".join(cleaned.split())

def ratio(a, b):
    return difflib.SequenceMatcher(None, norm(a), norm(b)).ratio()

def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False))
'''

DEFAULT_SSH_OPTS = [
    "-o", "BatchMode=yes",
    "-o", "ControlMaster=auto",
    "-o", "ControlPath=~/.ssh/plu-%r@%h:%p",
    "-o", "ControlPersist=300",
]


def run_remote(body: str, payload=None, config: dict | None = None, timeout: int = 300):
    """Run `body` on the library host with CONFIG/PAYLOAD bound, return its JSON."""
    config = config or load_config()
    header = "import json\nCONFIG = json.loads(%r)\nPAYLOAD = json.loads(%r)\n" % (
        json.dumps(config), json.dumps(payload if payload is not None else {}))
    source = header + REMOTE_PRELUDE + "\n" + body

    cmd = ["ssh", *config.get("ssh_opts", DEFAULT_SSH_OPTS), config["host"], "python3", "-"]
    proc = subprocess.run(cmd, input=source.encode("utf-8"),
                          capture_output=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError("remote command failed (exit %d):\n%s" % (
            proc.returncode, proc.stderr.decode("utf-8", "replace").strip()))
    out = proc.stdout.decode("utf-8", "replace").strip()
    if not out:
        raise RuntimeError("remote command produced no output:\n%s" %
                           proc.stderr.decode("utf-8", "replace").strip())
    try:
        return json.loads(out)
    except ValueError:
        raise RuntimeError("remote command produced non-JSON output:\n%s" % out[:2000])


def scp_from_remote(remote_path: str, local_path: Path, config: dict | None = None):
    """Copy one file down.

    Quoting depends on which protocol scp speaks, and library filenames contain
    spaces, so this matters:

    - OpenSSH >= 9 defaults to SFTP, which never invokes a remote shell. The
      path must be passed *raw*; shell-quoting it makes the quote characters
      part of the filename and the copy fails with "No such file or directory".
    - Older scp (and `-O`) does go through the remote shell, so the path must
      be quoted for it.

    Try the modern form first, fall back to the legacy one.
    """
    config = config or load_config()
    ssh_opts = config.get("ssh_opts", DEFAULT_SSH_OPTS)
    attempts = (
        (["scp", *ssh_opts, "-q",
          "%s:%s" % (config["host"], remote_path), str(local_path)], "sftp"),
        (["scp", *ssh_opts, "-O", "-q",
          "%s:%s" % (config["host"], shlex.quote(remote_path)), str(local_path)], "legacy"),
    )
    errors = []
    for cmd, mode in attempts:
        proc = subprocess.run(cmd, capture_output=True, timeout=300)
        if proc.returncode == 0:
            return
        errors.append("[%s] %s" % (mode, proc.stderr.decode("utf-8", "replace").strip()))
    raise RuntimeError("scp failed:\n%s" % "\n".join(errors))


# --------------------------------------------------------------------------
# filenames
# --------------------------------------------------------------------------

FILENAME_RE = re.compile(r"^\[([^\]]*)\]\s*(.+)$")
HEAD_RE = re.compile(r"^(\d{4})\s*(.*)$")
UNSAFE_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def parse_filename(name: str) -> dict:
    """`[2025 EMNLP]SciEvent- Benchmarking ....pdf` -> year/venue/title.

    Values here are only *hints*. The library host is the authority: it has
    already been observed to correct typos like `[2925 NAACL]` and `[2025 ENMLP]`.
    """
    stem = name[:-4] if name.lower().endswith(".pdf") else name
    match = FILENAME_RE.match(stem)
    if not match:
        return {"year": None, "venue": None, "title": stem.strip()}
    head, title = match.group(1).strip(), match.group(2).strip()
    head_match = HEAD_RE.match(head)
    if head_match:
        return {"year": int(head_match.group(1)),
                "venue": head_match.group(2).strip() or None,
                "title": title}
    return {"year": None, "venue": head or None, "title": title}


# The library stores venues lowercase. Upper-casing them blindly yields
# `NEURIPS`/`ARXIV`, which clashes with how these venues are written
# everywhere else in a topic folder, so map the ones with a house style.
VENUE_CASE = {
    "NEURIPS": "NeurIPS", "ARXIV": "arXiv", "AISTATS": "AISTATS",
    "JMLR": "JMLR", "COLT": "COLT", "UAI": "UAI", "ICLR": "ICLR",
    "ICML": "ICML", "CVPR": "CVPR", "ICCV": "ICCV", "ECCV": "ECCV",
}


def build_filename(title: str, venue: str | None, year: int | None) -> str:
    """Render the naming convention: `[year VENUE]title.pdf`."""
    venue_up = (venue or "").upper()
    head = " ".join(str(p) for p in (year, VENUE_CASE.get(venue_up, venue_up) or None) if p)
    safe = UNSAFE_RE.sub("-", (title or "untitled").strip()).strip(". ")
    safe = re.sub(r"\s+", " ", safe)[:180].strip()
    return "[%s]%s.pdf" % (head, safe) if head else "%s.pdf" % safe


def sanitize_dirname(name: str) -> str:
    """Category name -> relative folder path.

    A category may be a `/`-separated path (`"1 Retrieval/1.1 Local"`), which
    becomes nested folders. Each segment is sanitized on its own so the
    separator survives; empty segments are dropped.
    """
    segments = []
    for raw in (name or "").split("/"):
        seg = UNSAFE_RE.sub("-", raw.strip()).strip(". ")
        if seg:
            segments.append(seg)
    return "/".join(segments) or "Uncategorized"


def norm_title(text: str) -> str:
    cleaned = "".join(
        c if (c.isalnum() or c.isspace()) else " " for c in (text or "").lower())
    return " ".join(cleaned.split())


# --------------------------------------------------------------------------
# sidecar
# --------------------------------------------------------------------------

class Sidecar:
    """`<topic>/.paperlevelup/` -- papers.jsonl plus the topic's own taxonomy.

    Records are keyed by PDF basename, which survives moves between category
    subfolders.

    Identity is a dict, `ids: {archer, openalex, doi, arxiv}`, because with more
    than one backend no single id is "the" identity: archer knows where the PDF
    is, OpenAlex knows the DOI and the global citation count. `archer_id` is
    still written and read as a mirror of `ids["archer"]` so older sidecars and
    any code that predates sources keep working.
    """

    #: cache lives beside the sidecar; sources namespace themselves inside it
    CACHE_DIR = "cache"

    def __init__(self, topic_dir: Path):
        self.topic = Path(topic_dir).expanduser().resolve()
        self.dir = self.topic / SIDECAR_DIR
        self.papers_path = self.dir / PAPERS_FILE
        self.taxonomy_path = self.dir / TAXONOMY_FILE
        self.cache_dir = self.dir / self.CACHE_DIR
        self.entries: dict[str, dict] = {}
        self.taxonomy: dict = {"categories": []}
        self.load()

    def load(self):
        if self.papers_path.exists():
            for line in self.papers_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    rec = json.loads(line)
                    self.entries[rec["key"]] = _migrate_record(rec)
        if self.taxonomy_path.exists():
            self.taxonomy = json.loads(self.taxonomy_path.read_text(encoding="utf-8"))

    def save(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps(self.entries[k], ensure_ascii=False)
                 for k in sorted(self.entries)]
        self.papers_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def save_taxonomy(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        self.taxonomy_path.write_text(
            json.dumps(self.taxonomy, ensure_ascii=False, indent=2), encoding="utf-8")

    def upsert(self, key: str, **fields):
        rec = self.entries.setdefault(key, {"key": key, "added_at": now_iso()})
        rec.update(fields)
        if "archer_id" in fields:
            rec.setdefault("ids", {})["archer"] = fields["archer_id"]
            if not fields["archer_id"]:
                rec["ids"].pop("archer", None)
        rec["updated_at"] = now_iso()
        return rec

    def set_id(self, key: str, source: str, ident: str | None):
        """Record one source's identity without disturbing the others."""
        rec = self.entries.setdefault(key, {"key": key, "added_at": now_iso()})
        ids = rec.setdefault("ids", {})
        if ident:
            ids[source] = ident
        else:
            ids.pop(source, None)
        if source == "archer":
            rec["archer_id"] = ident
        rec["updated_at"] = now_iso()
        return rec

    def category_names(self) -> list[str]:
        return [c["name"] for c in self.taxonomy.get("categories", [])]

    def known_archer_ids(self) -> list[str]:
        return [r["ids"]["archer"] for r in self.entries.values()
                if (r.get("ids") or {}).get("archer")]

    def known_uids(self, source: str | None = None) -> list[str]:
        """Namespaced ids across every source, or just one of them."""
        out = []
        for rec in self.entries.values():
            for name, ident in (rec.get("ids") or {}).items():
                if ident and (source is None or name == source):
                    out.append("%s:%s" % (name, ident))
        return out

    def uid_to_key(self) -> dict[str, str]:
        out = {}
        for key, rec in self.entries.items():
            for name, ident in (rec.get("ids") or {}).items():
                if ident:
                    out["%s:%s" % (name, ident)] = key
        return out

    def known_dois(self) -> set[str]:
        return {(r.get("ids") or {}).get("doi", "").lower()
                for r in self.entries.values()
                if (r.get("ids") or {}).get("doi")}

    def known_titles(self) -> set[str]:
        out = set()
        for rec in self.entries.values():
            for field in ("title", "filename_title"):
                if rec.get(field):
                    out.add(norm_title(rec[field]))
        return out


def _migrate_record(rec: dict) -> dict:
    """Bring a pre-sources record up to the `ids` schema, in memory.

    Older sidecars carry a bare `archer_id`. Lifting it into `ids` on load
    means nothing has to be rewritten on disk until the next save, and a
    sidecar written by the newer code still reads on the older one because
    `archer_id` stays populated.
    """
    ids = rec.get("ids")
    if not isinstance(ids, dict):
        ids = {}
    if rec.get("archer_id") and not ids.get("archer"):
        ids["archer"] = rec["archer_id"]
    rec["ids"] = ids
    return rec


def scan_pdfs(topic_dir: Path) -> tuple[list[dict], list[str]]:
    """Find every PDF under the topic folder, including inside category
    subfolders. Returns (papers, problems)."""
    topic = Path(topic_dir).expanduser().resolve()
    if not topic.is_dir():
        raise SystemExit("topic folder not found: %s" % topic)

    found: dict[str, dict] = {}
    problems: list[str] = []
    for path in sorted(topic.rglob("*.pdf")):
        rel = path.relative_to(topic)
        if any(part.startswith(".") for part in rel.parts):
            continue
        if path.name.startswith("._"):
            continue
        key = path.name
        if key in found:
            problems.append("duplicate basename in two folders: %s and %s" % (
                found[key]["path"], rel.as_posix()))
            continue
        found[key] = {"key": key, "path": rel.as_posix(),
                      "category_dir": "/".join(rel.parts[:-1]) or None}
    return list(found.values()), problems


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def add_topic_arg(parser):
    parser.add_argument("--topic", required=True,
                        help="path to the topic folder holding the PDFs")
    return parser


def print_json(obj):
    json.dump(obj, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
