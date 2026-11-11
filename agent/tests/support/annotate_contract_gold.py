"""Independent source reading annotations, authored before any corpus model calls.

The fixed seed selection is pinned by source filename and source hash. Labels never
provide numeric answers. Unknown contextual/conditional values are unscored.
"""

from pathlib import Path

from agent.tests.support.contract_corpus import DEFAULT_OUTPUT, FIELDS, gold_field, read_json, write_json

# (field, value, unit, original paragraph ID). These are manual source annotations.
PRESENT = {
    1: [("pay_type", "hourly", "category", 23), ("contract_rate", "9620", "KRW/hour", 23)],
    3: [
        ("pay_type", "annual", "category", 22),
        ("contract_start", "2022-11-01", "date", 7),
        ("contract_end", "2023-12-31", "date", 7),
        ("work_start", "10:00", "time", 17),
        ("work_end", "19:00", "time", 17),
        ("payday", "10", "day_of_month", 26),
        ("scheduled_daily_minutes", "480", "minute", 12),
    ],
    5: [("pay_type", "monthly", "category", 11)],
    7: [
        ("pay_type", "annual", "category", 91),
        ("work_start", "09:00", "time", 24),
        ("work_end", "17:00", "time", 24),
        ("payday", "25", "day_of_month", 36),
        ("scheduled_daily_minutes", "480", "minute", 22),
        ("scheduled_weekly_minutes", "2400", "minute", 22),
    ],
    9: [("pay_type", "daily", "category", 12), ("payday", "10", "day_of_month", 17)],
    10: [
        ("pay_type", "annual", "category", 15),
        ("work_start", "09:00", "time", 36),
        ("work_end", "18:00", "time", 36),
        ("break_minutes", "60", "minute", 37),
        ("payday", "25", "day_of_month", 28),
    ],
    12: [
        ("pay_type", "annual", "category", 47),
        ("work_start", "10:00", "time", 33),
        ("work_end", "19:00", "time", 33),
        ("scheduled_daily_minutes", "480", "minute", 32),
    ],
    14: [("pay_type", "hourly", "category", 14)],
    15: [("pay_type", "hourly", "category", 15)],
    16: [("scheduled_daily_minutes", "480", "minute", 4)],
    18: [("pay_type", "annual", "category", 48), ("payday", "last_day", "day_of_month", 48)],
    20: [("pay_type", "annual", "category", 33), ("payday", "10", "day_of_month", 38)],
}
AMBIGUOUS = {
    1: {"break_minutes": (15, "Conditional 4h/8h statutory guidance, not a selected daily break")},
    7: {"break_minutes": (27, "Only a clock interval is provided; do not infer minutes")},
    10: {"contract_end": (30, "2023-12-31 is wage-period end, not employment contract end")},
    11: {"pay_type": (23, "Annual wage mentioned only in optional wage-increase clause")},
    12: {"break_minutes": (33, "Only a clock interval is provided; do not infer minutes")},
    13: {
        "pay_type": (25, "Daily-based monthly compensation conflicts with blank hourly clause 32"),
        "contract_rate": (25, "50000 KRW/day formula and blank hourly wage; no single confirmed rate"),
        "payday": (67, "Relative first payment after 30 days; no calendar day filled"),
    },
    16: {"pay_type": (18, "Unselected hourly/monthly alternatives")},
    17: {"pay_type": (15, "Revenue share with blank percentage, outside supported fixed rate taxonomy")},
    19: {
        "pay_type": (13, "Event fee outside supported fixed wage taxonomy"),
        "payday": (13, "Contradictory relative wording: 행사 후 30일 전까지"),
    },
}


def annotate(output=DEFAULT_OUTPUT):
    manifest = read_json(output / "manifest.json")
    pinned = read_json(Path(__file__).with_name("contract_gold_sources.json"))
    for doc in manifest["documents"]:
        if doc["kind"] != "json":
            continue
        index = int(doc["id"].split("-")[1])
        mapping = read_json(output / "mappings" / (doc["id"] + ".json"))
        if (
            Path(doc["source"]).name != pinned[doc["id"]]["filename"]
            or mapping["source_hash"] != pinned[doc["id"]]["source_hash"]
        ):
            raise ValueError("Source selection/content changed; independent annotation review required")
        paragraphs = {p["paragraph_id"]: p for p in mapping["paragraphs"]}
        fields = {
            key: gold_field("absent", source="Source reviewed: blank, placeholder, or not stated")
            for key in FIELDS
        }
        for field, value, unit, pid in PRESENT.get(index, []):
            paragraph = paragraphs[pid]
            fields[field] = gold_field("present", value, unit, paragraph["text"], paragraph["pages"][0])
            fields[field]["paragraph_id"] = pid
        for field, (pid, reason) in AMBIGUOUS.get(index, {}).items():
            paragraph = paragraphs[pid]
            fields[field] = gold_field("unscored", source=paragraph["text"], page=paragraph["pages"][0])
            fields[field].update(reason=reason, paragraph_id=pid)
        write_json(
            output / "gold" / (doc["id"] + ".json"),
            {
                "annotation_version": "human-source-v1",
                "reviewed_before_calls": True,
                "source_hash": mapping["source_hash"],
                "pdf_hash": doc["sha256"],
                "method": "Manual reading of ordered source paragraphs; section labels used only for discovery",
                "fields": fields,
            },
        )
    print(
        "Independent annotations written for 20 JSON documents; 14 blank + 3 synthetic annotations retained."
    )


if __name__ == "__main__":
    annotate()
