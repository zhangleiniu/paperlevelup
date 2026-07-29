"""Pluggable metadata sources.

Why this exists
---------------
The library host (archer) covers 15 AI/ML venues. A typical topic folder
resolves ~50% against it; the rest are SIGIR/KDD/TKDE/ECIR/journal/arXiv
papers the corpus simply does not carry. Measured on Sentific_literature:
25 of 49 papers were invisible to archer, and OpenAlex matched 25/25 of them.

The sources are **not interchangeable** -- they are authoritative about
different things:

                 archer          OpenAlex           arXiv
    PDF          all             ~43% (OA only)     preprints
    abstract     ~96%            ~76%               yes
    references   3.8M internal   ~48% (arXiv gap)   no
    venues       15              all                preprints
    global cites no              yes                no
    DOI          no              ~84%

So a source declares which capabilities it actually has, and callers ask for
a capability rather than assuming one backend can do everything.

Capabilities
------------
    resolve   filename/title hint -> a confident identity + metadata
    search    keyword query -> ranked candidates
    refs      paper id -> the works it cites
    cites     paper id -> newer works citing it
    pdf       paper id -> a downloaded file

Identity
--------
Records carry a namespaced `uid` (`archer:<uuid>`, `openalex:W123`) so ids
from different sources can travel in the same list without colliding. A bare
id with no prefix is read as archer's, which keeps older invocations working.

Judgement boundary
------------------
These modules do retrieval and deterministic bookkeeping only: dedup by DOI or
normalised title, field-level merge by a fixed precedence. They never score
relevance and never fuse rankings across sources -- candidates come back
tagged with their provenance so the agent can judge them in conversation.
That is the same division of labour the rest of the skill follows.
"""
from .base import (Source, SourceError, merge_records, normalise_title,
                   split_uid, make_uid)

_REGISTRY: dict[str, type] = {}


def register(cls):
    _REGISTRY[cls.name] = cls
    return cls


def available() -> list[str]:
    _load_all()
    return sorted(_REGISTRY)


def get_source(name: str, config: dict | None = None, cache_dir=None) -> Source:
    _load_all()
    if name not in _REGISTRY:
        raise SourceError("unknown source %r; available: %s"
                          % (name, ", ".join(sorted(_REGISTRY))))
    return _REGISTRY[name](config=config, cache_dir=cache_dir)


def sources_with(capability: str, names, config=None, cache_dir=None) -> list[Source]:
    """Instantiate the named sources that actually support `capability`."""
    out = []
    for name in names:
        src = get_source(name, config=config, cache_dir=cache_dir)
        if capability in src.capabilities:
            out.append(src)
    return out


def _load_all():
    if _REGISTRY:
        return
    from . import archer, openalex  # noqa: F401  (import registers them)


__all__ = ["Source", "SourceError", "register", "available", "get_source",
           "sources_with", "merge_records", "normalise_title", "split_uid",
           "make_uid"]
