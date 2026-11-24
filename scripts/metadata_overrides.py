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


#: provenance for a field whose override was withdrawn but whose earlier value
#: was never recorded (ledgers applied before `displaced` existed)
WITHDRAWN = "withdrawn_override"


def withdraw_overrides(sidecar, data: dict) -> list[dict]:
    """Undo override fields that are no longer in the ledger.

    Without this, `--remove` (or a `--set` that drops a field) only edits the
    ledger: papers.jsonl keeps the withdrawn value, still labelled
    `metadata_override`, until a full re-resolve. Each applied override keeps
    the value it displaced, so withdrawing restores that value and its source.
    """
    records = data.get("records", {})
    withdrawn = []
    for key in sorted(sidecar.entries):
        rec = sidecar.entries[key]
        marker = rec.get("metadata_override")
        if not marker:
            continue
        entry = records.get(key) or {}
        displaced = marker.get("displaced") or {}
        provenance = rec.setdefault("provenance", {})
        restored, stale = [], []
        for field in marker.get("fields", []):
            if field in entry or provenance.get(field) != "metadata_override":
                continue
            if field in displaced:
                rec[field] = displaced[field].get("value")
                if displaced[field].get("provenance"):
                    provenance[field] = displaced[field]["provenance"]
                else:
                    provenance.pop(field, None)
                restored.append(field)
            else:
                provenance[field] = WITHDRAWN
                stale.append(field)
        if key not in records:
            del rec["metadata_override"]
        if restored or stale:
            withdrawn.append({"key": key, "restored": restored,
                              **({"stale": stale} if stale else {})})
    return withdrawn


def apply_overrides(sidecar, data: dict | None = None) -> list[dict]:
    data = data if data is not None else load_overrides(sidecar)
    validate_ledger(data, set(sidecar.entries))
    withdraw_overrides(sidecar, data)
    applied = []
    for key, entry in sorted(data.get("records", {}).items()):
        fields = {name: entry[name] for name in ALLOWED_FIELDS if name in entry}
        rec = sidecar.entries[key]
        provenance = rec.get("provenance") or {}
        displaced = dict((rec.get("metadata_override") or {}).get("displaced") or {})
        for field in fields:
            # Only a value some source (or nothing) put there is worth keeping;
            # one still marked as the override is the override itself.
            if provenance.get(field) not in ("metadata_override", WITHDRAWN):
                displaced[field] = {"value": rec.get(field),
                                    "provenance": provenance.get(field)}
        rec = sidecar.upsert(key, **fields)
        provenance = rec.setdefault("provenance", {})
        for field in fields:
            provenance[field] = "metadata_override"
        rec["metadata_override"] = {
            "fields": sorted(fields),
            "evidence": entry["evidence"],
            **({"note": entry["note"]} if entry.get("note") else {}),
            "displaced": {f: v for f, v in displaced.items() if f in fields},
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
