import json
import shutil
import subprocess
from pathlib import Path

import pytest

from agent.services.extraction_profiles import PROFILES


def test_three_routes_require_separate_ocr_and_explicit_ai_processing():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the UI initialization regression test")
    result = subprocess.run(
        [
            node,
            str(Path(__file__).parent / "web/document_types.cjs"),
            json.dumps(PROFILES, ensure_ascii=False),
        ],
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")


def test_material_linking_review_and_evidence_ui():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the UI initialization regression test")
    result = subprocess.run(
        [node, str(Path(__file__).parent / "web/comparison_ui.cjs")],
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")


def test_field_validation_review_keeps_uncertain_suggestions_out_of_inputs():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the UI review regression test")
    result = subprocess.run(
        [
            node,
            str(Path(__file__).parent / "web/field_review.cjs"),
            json.dumps(PROFILES, ensure_ascii=False),
        ],
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")


def test_independent_comparison_workspace_and_single_action():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the comparison workspace test")
    result = subprocess.run(
        [
            node,
            str(Path(__file__).parent / "web/comparison_workspace.cjs"),
            json.dumps(PROFILES, ensure_ascii=False),
        ],
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")


def test_explicit_supplement_and_candidate_selection_ui():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the supplement UI test")
    result = subprocess.run(
        [
            node,
            str(Path(__file__).parent / "web/supplement_ui.cjs"),
            json.dumps(PROFILES, ensure_ascii=False),
        ],
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
