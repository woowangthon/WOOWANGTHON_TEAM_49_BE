import re

from agent.services.extraction_profiles import DOCUMENT_TYPES, PROFILES, canonical_document_type


class FixtureProvider:
    """Explicit synthetic provider; never used as an automatic fallback."""

    async def invoke(self, operation, data):
        if operation == "explain":
            return {"templates": ["확인 결과: {{summary}}"], "question_ids": data["question_ids"]}, {}
        fields = []
        for page, text in enumerate(data.get("pages", []), 1):
            for line in text.splitlines():
                match = re.match(r"([a-z_]+):\s*(\d+(?:\.\d+)?)\s*(\S+)", line.strip())
                if match:
                    name, value, unit = match.groups()
                    fields.append(
                        {
                            "field_id": name,
                            "raw_value": value,
                            "normalized_value": value,
                            "unit": unit,
                            "page": page,
                            "quote": line.strip(),
                            "issues": [],
                        }
                    )
        document_type = canonical_document_type(data["document_type"])
        issues = []
        if document_type == "auto":
            # Development fixtures use explicit field names. This intentionally
            # does not pretend to be a classifier for arbitrary real documents.
            detected = {
                kind
                for kind in DOCUMENT_TYPES
                if any(item["field_id"] in PROFILES[kind]["fields"] for item in fields)
            }
            if len(detected) == 1:
                document_type = detected.pop()
            else:
                document_type, fields = "unknown", []
                issues.append("FIXTURE_DOCUMENT_TYPE_UNRESOLVED")
        return {"document_type": document_type, "fields": fields, "issues": issues}, {}
