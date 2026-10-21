"""The Word-made test documents are complete and consistent."""

import json
from pathlib import Path

import pymupdf
import pytest

WORD = Path(__file__).parent / "fixtures" / "word"
DOCS = ["arabic-quotation", "french-quote", "bilingual-contract", "english-styles"]
EXPECTED = json.loads((WORD / "expected.json").read_text(encoding="utf-8"))
EDITS = json.loads((WORD / "edits.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", DOCS)
def test_pdf_was_made_by_word(name):
    with pymupdf.open(WORD / f"{name}-word.pdf") as doc:
        assert doc.page_count == 2
        assert "Microsoft" in doc.metadata["producer"] and "Word" in doc.metadata["producer"]


@pytest.mark.parametrize("edit", EDITS, ids=lambda e: f"{e['doc']}-{e['kind']}")
def test_every_planned_edit_points_at_one_paragraph(edit):
    matches = [p["text"] for p in EXPECTED if p["doc"] == edit["doc"] and edit["find"] in p["text"]]
    assert len(matches) == 1
    assert edit["old"] in matches[0]
