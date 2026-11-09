"""Persistent, evidence-bearing metadata corrections for paperlevelup topics."""

from __future__ import annotations

import json
from pathlib import Path


OVERRIDES_FILE = "metadata-overrides.json"
ALLOWED_FIELDS = ("title", "authors", "venue", "year", "doi", "url")


def overrides_path(sidecar) -> Path:
    return sidecar.dir / OVERRIDES_FILE


def load_overrides(sidecar) -> dict:
    path = overrides_path(sidecar)
    if not path.exists():
        return {"version": 1, "records": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    validate_ledger(data, set(sidecar.entries))
    return data


def save_overrides(sidecar, data: dict) -> Path:
    validate_ledger(data, set(sidecar.entries))
    path = overrides_path(sidecar)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
    return path


def normalize_entries(payload) -> list[dict]:
    if isinstance(payload, dict) and "records" in payload:
        records = payload["records"]
        if not isinstance(records, dict):
            raise ValueError("records must be an object keyed by exact PDF basename")
        return [dict(value, key=key) for key, value in records.items()]
    if isinstance(payload, dict):
        return [payload]
    if isinstance(payload, list):
        return payload
    raise ValueError("metadata payload must be an object, list, or ledger object")


def set_entries(sidecar, data: dict, entries: list[dict]) -> list[str]:
    records = data.setdefault("records", {})
    changed = []
    for raw in entries:
        entry = normalize_entry(raw, set(sidecar.entries))
        key = entry.pop("key")
        records[key] = entry
        changed.append(key)
    validate_ledger(data, set(sidecar.entries))
    return sorted(changed)


def remove_entries(data: dict, keys: list[str]) -> list[str]:
    records = data.setdefault("records", {})
    removed = []
    for key in keys:
        if key in records:
            del records[key]
            removed.append(key)
    return sorted(removed)


def apply_overrides(sidecar, data: dict | None = None) -> list[dict]:
    data = data if data is not None else load_overrides(sidecar)
    validate_ledger(data, set(sidecar.entries))
    applied = []
    for key, entry in sorted(data.get("records", {}).items()):
        fields = {name: entry[name] for name in ALLOWED_FIELDS if name in entry}
        rec = sidecar.upsert(key, **fields)
        provenance = rec.setdefault("provenance", {})
        for field in fields:
            provenance[field] = "metadata_override"
        rec["metadata_override"] = {
            "fields": sorted(fields),
            "evidence": entry["evidence"],
            **({"note": entry["note"]} if entry.get("note") else {}),
        }
        applied.append({"key": key, "fields": sorted(fields)})
    return applied


def normalize_entry(raw: dict, known_keys: set[str]) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("every metadata override must be an object")
    key = raw.get("key")
    if not isinstance(key, str) or not key.strip():
        raise ValueError("every metadata override requires key")
    if key not in known_keys:
        raise ValueError("override key is not an exact topic PDF basename: %s" % key)
    evidence = raw.get("evidence")
    if not isinstance(evidence, str) or not evidence.strip():
        raise ValueError("metadata override for %s requires evidence" % key)

    unknown = set(raw) - set(ALLOWED_FIELDS) - {"key", "evidence", "note"}
    if unknown:
        raise ValueError("unsupported metadata fields for %s: %s" %
                         (key, ", ".join(sorted(unknown))))
    fields = {name: raw[name] for name in ALLOWED_FIELDS if name in raw}
    if not fields:
        raise ValueError("metadata override for %s changes no fields" % key)
    validate_fields(key, fields)
    result = {"key": key, **fields, "evidence": evidence.strip()}
    if raw.get("note"):
        result["note"] = str(raw["note"]).strip()
    return result


def validate_ledger(data: dict, known_keys: set[str]) -> None:
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError("metadata override ledger must have version 1")
    records = data.get("records")
    if not isinstance(records, dict):
        raise ValueError("metadata override ledger requires a records object")
    for key, entry in records.items():
        normalize_entry({"key": key, **entry}, known_keys)


def validate_fields(key: str, fields: dict) -> None:
    authors = fields.get("authors")
    if authors is not None:
        if (not isinstance(authors, list) or not authors or
                any(not isinstance(a, str) or not a.strip() for a in authors)):
            raise ValueError("authors for %s must be a non-empty string list" % key)
    year = fields.get("year")
    if year is not None and (not isinstance(year, int) or year < 1800 or year > 2200):
        raise ValueError("year for %s must be an integer between 1800 and 2200" % key)
    for field in ("title", "venue", "doi", "url"):
        value = fields.get(field)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError("%s for %s must be a non-empty string" % (field, key))
