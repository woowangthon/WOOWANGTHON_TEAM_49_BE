from agent.rules.registry import WAGE_RULE_IDS


def support_manifest(registry):
    cells = []
    for profile in ("H-STD", "M-STD"):
        for rule_id in (*WAGE_RULE_IDS, "R-07"):
            rules = [r for r in registry.rules if r.profile == profile and r.rule_id == rule_id]
            verified = bool(rules) and all(r.verification_status == "passed" for r in rules)
            if rule_id == "R-07":
                verified = bool(getattr(registry, "supported_r07", False))
                if verified:
                    rules = [r for r in registry.rules if r.profile == profile]
            cells.append(
                {
                    "cell_id": f"{profile}/{rule_id}",
                    "required_for_release": True,
                    "verification_status": "passed" if verified else "pending",
                    "rule_versions": sorted({r.version for r in rules}),
                    "official_sources": sorted({source for r in rules for source in r.official_sources}),
                    "verification_cases": sorted({case for r in rules for case in r.verification_cases}),
                    "effective_periods": [
                        {"start": str(r.effective_from), "end": str(r.effective_to)} for r in rules
                    ],
                    "rounding": "ROUND_HALF_UP at each final won amount; no intermediate rounding"
                    if verified
                    else None,
                }
            )
    return {
        "manifest_version": registry.manifest_version,
        "cells": cells,
        "release_ready": all(cell["verification_status"] == "passed" for cell in cells),
        "scope": "Rule matrix only; not a claim of production/AI accuracy readiness",
        "reason": "Confirmed unchanged standard schedules only; unsupported conditions remain explicit",
    }
