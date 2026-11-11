import hashlib
import json
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, SecretStr

SET_FIELDS = {
    "confirmed_fields",
    "condition_states",
    "authorized_sources",
    "evidence_catalog",
    "earnings",
    "deductions",
    "transaction_allocations",
    "work_records",
    "coverage",
    "context_requirements",
    "evidence_refs",
    "dependencies",
    "breaks",
    "completed_nodes",
    "reason_codes",
}


def canonical(value, *, unordered=False):
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="python")
    if isinstance(value, SecretStr):
        raise ValueError("Secrets cannot enter content hashes")
    if isinstance(value, dict):
        return {k: canonical(v, unordered=k in SET_FIELDS) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        items = [canonical(v) for v in value]
        return sorted(items, key=lambda v: json.dumps(v, sort_keys=True)) if unordered else items
    if isinstance(value, Decimal):
        return "0" if value == 0 else format(value.normalize(), "f")
    if isinstance(value, datetime):
        from datetime import UTC

        return value.astimezone(UTC).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def digest(value) -> str:
    return hashlib.sha256(
        json.dumps(canonical(value), ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
    ).hexdigest()


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def snapshot_hash(snapshot) -> str:
    data = snapshot.model_dump(exclude={"snapshot_hash", "snapshot_id"})
    # Additive defaults must not rewrite signed snapshots saved before wage support.
    if data.get("wage_conditions") is None:
        data.pop("wage_conditions", None)
    return digest(data)


def result_hash(result) -> str:
    data = result.model_dump(
        exclude={"result_hash", "result_id", "result_version", "freshness", "created_at"}
    )
    defaults = {
        "included_amount": None,
        "additional_amount": None,
        "payable_amount": None,
        "required_field_ids": [],
        "required_periods": [],
        "official_sources": [],
        "dependencies": [],
    }
    for item in data["wage_section"]["items"]:
        for key, default in defaults.items():
            if item.get(key) == default:
                item.pop(key, None)
    for item in data["questions"]:
        for key, default in {"reason": "", "required_periods": [], "glossary_ids": []}.items():
            if item.get(key) == default:
                item.pop(key, None)
    for item in data["comparisons"]:
        if not item.get("wage_items"):
            item.pop("wage_items", None)
    return digest(data)
