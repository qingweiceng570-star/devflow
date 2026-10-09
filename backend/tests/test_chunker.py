from app.rag.chunker import CHUNKER_VERSION, chunk_document, parse_blocks


def test_blocks_and_headings(settings):
    text = "# Title\n\nintro paragraph\n\n## Section A\n\ncontent a\n\n- item 1\n- item 2\n"
    blocks = parse_blocks(text)
    kinds = [block.kind for block in blocks]
    assert kinds == ["heading", "paragraph", "heading", "paragraph", "list"]
    chunks = chunk_document(text, settings)
    assert chunks[0].heading_path == ["Title"]
    assert chunks[-1].heading_path == ["Title", "Section A"]
    assert chunks[-1].text.startswith("## Section A")
    assert CHUNKER_VERSION == "structure.v1"


def test_code_block_kept_intact(settings):
    text = "# T\n\nbefore\n\n```python\nprint('hello')\nprint('world')\n```\n\nafter\n"
    chunks = chunk_document(text, settings)
    code_chunk = next(chunk for chunk in chunks if "print('hello')" in chunk.text)
    assert "print('world')" in code_chunk.text
    assert code_chunk.text.count("```") == 2


def test_oversized_code_splits_by_lines_inheriting_heading(settings):
    settings.rag_chunk_target_tokens = 60
    settings.rag_chunk_max_tokens = 80
    body = "\n".join(f"line_{i} = {i}" for i in range(80))
    text = f"# Big\n\n```python\n{body}\n```\n"
    chunks = chunk_document(text, settings)
    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.heading_path == ["Big"]
        assert chunk.token_estimate <= 120  # max plus the heading line
    joined = "\n".join(chunk.text for chunk in chunks)
    assert "line_0 = 0" in joined and "line_79 = 79" in joined


def test_line_ranges_and_overlap(settings):
    settings.rag_chunk_target_tokens = 40
    settings.rag_chunk_max_tokens = 60
    settings.rag_chunk_overlap_tokens = 10
    paragraphs = "\n\n".join(f"paragraph number {i} with some words here" for i in range(12))
    text = f"# Doc\n\n{paragraphs}\n"
    chunks = chunk_document(text, settings)
    assert len(chunks) > 2
    for first, second in zip(chunks, chunks[1:], strict=False):
        assert first.line_start <= first.line_end
        assert second.line_start >= first.line_start
    # Overlap: consecutive chunks share their boundary text.
    assert any(
        first.text.splitlines()[-1] in second.text
        for first, second in zip(chunks, chunks[1:], strict=False)
    )


def test_huge_single_line_splits_on_sentences(settings):
    settings.rag_chunk_target_tokens = 30
    settings.rag_chunk_max_tokens = 40
    line = "".join(f"这是第{i}句说明文字。" for i in range(40))
    chunks = chunk_document(f"# S\n\n{line}\n", settings)
    assert len(chunks) > 1
    assert all(chunk.token_estimate <= 60 for chunk in chunks)


def test_empty_and_plain_text(settings):
    assert chunk_document("", settings) == []
    assert chunk_document("   \n\n  ", settings) == []
    chunks = chunk_document("just a line of text\n", settings)
    assert len(chunks) == 1 and chunks[0].heading_path == []
