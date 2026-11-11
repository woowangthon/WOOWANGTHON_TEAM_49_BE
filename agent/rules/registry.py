from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from agent.schemas.public.errors import IntegrityError
from agent.schemas.public.results import WageItemResult

WAGE_RULE_IDS = tuple(f"W-0{i}" for i in range(1, 6))


@dataclass(frozen=True)
class VerifiedRule:
    rule_id: str
    version: str
    profile: str
    effective_from: date
    effective_to: date
    required_fields: tuple[str, ...]
    official_sources: tuple[str, ...]
    verification_cases: tuple[str, ...]
    verification_status: str
    formula_id: str
    calculate: Callable
    quantum: Decimal = Decimal("1")
    rounding: str = ROUND_HALF_UP


class RuleRegistry:
    def __init__(self, rules=(), bundle_version="pending-v1", manifest_version="pending-v1"):
        self.rules = list(rules)
        self.bundle_version = bundle_version
        self.manifest_version = manifest_version
        for rule in self.rules:
            if (
                rule.rule_id not in WAGE_RULE_IDS
                or rule.verification_status != "passed"
                or not rule.official_sources
                or not rule.verification_cases
                or rule.effective_to < rule.effective_from
                or rule.quantum <= 0
            ):
                raise ValueError("Only independently verified, source-backed rules can be registered")
        for i, left in enumerate(self.rules):
            for right in self.rules[i + 1 :]:
                if (
                    left.rule_id == right.rule_id
                    and left.profile == right.profile
                    and max(left.effective_from, right.effective_from)
                    <= min(left.effective_to, right.effective_to)
                ):
                    raise ValueError("Ambiguous rule coverage")

    def evaluate(self, rule_id, context):
        snapshot = context.snapshot
        candidates = [
            r
            for r in self.rules
            if r.rule_id == rule_id
            and r.profile == snapshot.profile
            and r.effective_from <= snapshot.period.start
            and r.effective_to >= snapshot.period.end
        ]
        if not candidates:
            return WageItemResult(rule_id=rule_id, status="needs_review", reason_codes=["RULE_UNVERIFIED"])
        rule = candidates[0]
        fields = {f.field_id: f for f in snapshot.confirmed_fields}
        selected = [fields.get(key) for key in rule.required_fields]
        conditions = {c.field_id: c.state for c in snapshot.condition_states}
        conflict = any(f is not None and f.state == "conflict" for f in selected) or any(
            conditions.get(key) == "conflict" for key in rule.required_fields
        )
        missing = any(f is None or f.state != "known" for f in selected) or any(
            conditions.get(key, "known") != "known" for key in rule.required_fields
        )
        if conflict or missing or context.issues:
            status = (
                "needs_review"
                if conflict or any(not x.startswith("missing_context") for x in context.issues)
                else "insufficient"
            )
            return WageItemResult(
                rule_id=rule_id,
                rule_version=rule.version,
                status=status,
                reason_codes=["CONDITION_CONFLICT" if conflict else "REQUIRED_INPUT_MISSING"],
            )
        amount = rule.calculate(context)
        if not isinstance(amount, Decimal) or not amount.is_finite():
            raise IntegrityError("Rule must return finite Decimal")
        return WageItemResult(
            rule_id=rule_id,
            rule_version=rule.version,
            status="calculated",
            amount=amount.quantize(rule.quantum, rounding=rule.rounding),
            formula_id=rule.formula_id,
            operands={f.field_id: str(f.value) for f in selected},
            evidence_refs=sorted({e for f in selected for e in f.evidence_refs}),
        )
