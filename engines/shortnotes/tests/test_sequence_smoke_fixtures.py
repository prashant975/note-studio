from __future__ import annotations

from pathlib import Path

from pypdf import PdfReader

from scripts.make_sequence_smoke_fixtures import MARKER, build_fixtures


def _page_texts(path: Path) -> list[str]:
    return [(page.extract_text() or "") for page in PdfReader(str(path)).pages]


def test_build_fixtures_creates_only_the_three_named_synthetic_pdfs(tmp_path) -> None:
    paths = build_fixtures(tmp_path)

    assert paths == {
        "text-order": tmp_path / "text-order.pdf",
        "one-diagram": tmp_path / "one-diagram.pdf",
        "three-diagrams": tmp_path / "three-diagrams.pdf",
    }
    assert {path.name for path in tmp_path.iterdir()} == {
        "text-order.pdf",
        "one-diagram.pdf",
        "three-diagrams.pdf",
    }
    assert all(path.is_file() for path in paths.values())


def test_text_order_fixture_keeps_intentionally_non_textbook_source_order(tmp_path) -> None:
    path = build_fixtures(tmp_path)["text-order"]
    texts = _page_texts(path)

    assert len(texts) == 1
    text = texts[0]
    assert text.index("Topic C") < text.index("Topic A") < text.index("Topic B")
    assert MARKER in text


def test_diagram_fixtures_have_expected_pages_labels_and_privacy_marker(tmp_path) -> None:
    paths = build_fixtures(tmp_path)
    one_texts = _page_texts(paths["one-diagram"])
    three_texts = _page_texts(paths["three-diagrams"])

    assert len(one_texts) == 1
    assert "Diagram One" in one_texts[0]
    assert "Node A" in one_texts[0]
    assert "Node B" in one_texts[0]
    assert len(three_texts) == 3
    assert [
        any(f"Diagram {index}" in text for text in three_texts)
        for index in range(1, 4)
    ] == [True, True, True]
    assert all(MARKER in text for text in [*one_texts, *three_texts])
