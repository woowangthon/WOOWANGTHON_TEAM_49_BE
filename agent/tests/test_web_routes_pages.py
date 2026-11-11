import json
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

from agent.services.extraction_profiles import PROFILES


def test_eight_routes_keep_versions_drafts_and_explicit_actions_separate():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the UI regression test")
    result = subprocess.run(
        [
            node,
            str(Path(__file__).parent / "web/routes_pages.cjs"),
            json.dumps(PROFILES, ensure_ascii=False),
        ],
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")


def test_result_question_inquiry_and_report_sections_are_independent():
    class Sections(HTMLParser):
        def __init__(self):
            super().__init__()
            self.stack = []
            self.parents = {}
            self.routes = set()

        def handle_starttag(self, tag, attributes):
            attributes = dict(attributes)
            if attributes.get("data-route"):
                self.routes.add(attributes["data-route"])
            if tag == "section":
                identifier = attributes.get("id")
                if identifier:
                    self.parents[identifier] = self.stack.copy()
                self.stack.append(identifier)

        def handle_endtag(self, tag):
            if tag == "section" and self.stack:
                self.stack.pop()

    parser = Sections()
    parser.feed((Path(__file__).parents[1] / "dev/web/index.html").read_text(encoding="utf-8"))
    assert parser.routes == {"ocr", "ai", "compare", "results", "questions", "inquiry", "reports", "glossary"}
    for route in ["results", "questions", "inquiry", "reports", "glossary"]:
        assert parser.parents[f"{route}-page"] == []
