from __future__ import annotations

import re

import pytest

from ingestion_service.domain.entities import ParsedBlock, ParsedDocument
from ingestion_service.domain.exceptions import ValidationError
from ingestion_service.infrastructure.chunking import ChunkingConfig, StructureAwareChunker


class WhitespaceTokenCounter:
    def count(self, text: str) -> int:
        return len(re.findall(r"\S+", text))


class CharacterTokenCounter:
    def count(self, text: str) -> int:
        return len(text)


def parsed_document(*blocks: ParsedBlock) -> ParsedDocument:
    return ParsedDocument(
        blocks=tuple(blocks),
        parser_name="test",
        parser_version="1",
        mime_type="text/plain",
    )


def test_chunking_is_deterministic_and_respects_token_limits() -> None:
    content = " ".join(f"word-{index}" for index in range(30))
    document = parsed_document(ParsedBlock(content, section_path="Policy"))
    chunker = StructureAwareChunker(
        WhitespaceTokenCounter(),
        ChunkingConfig(target_tokens=10, max_tokens=12, overlap_tokens=2),
    )

    first = chunker.chunk(document)
    second = chunker.chunk(document)

    assert first == second
    assert len(first) > 1
    assert all(0 < chunk.token_count <= 12 for chunk in first)
    assert all(chunk.section_path == "Policy" for chunk in first)


def test_overlap_is_applied_only_within_the_same_section_and_page() -> None:
    first_page = " ".join(f"p1-{index}" for index in range(18))
    second_page = " ".join(f"p2-{index}" for index in range(6))
    document = parsed_document(
        ParsedBlock(first_page, section_path="Rules", page_number=1),
        ParsedBlock(second_page, section_path="Rules", page_number=2),
        ParsedBlock("private material", section_path="Restricted", page_number=2),
    )
    chunker = StructureAwareChunker(
        WhitespaceTokenCounter(),
        ChunkingConfig(target_tokens=8, max_tokens=10, overlap_tokens=2),
    )

    chunks = chunker.chunk(document)

    page_one_chunks = [chunk for chunk in chunks if chunk.page_number == 1]
    assert len(page_one_chunks) > 1
    previous_words = page_one_chunks[0].content.split("\n\n", 1)[1].split()
    next_words = page_one_chunks[1].content.split("\n\n", 1)[1].split()
    assert next_words[:2] == previous_words[-2:]
    assert all("p1-" not in chunk.content for chunk in chunks if chunk.page_number == 2)
    restricted = [chunk for chunk in chunks if chunk.section_path == "Restricted"]
    assert restricted and all("p2-" not in chunk.content for chunk in restricted)


def test_blocks_in_the_same_structure_are_packed_together() -> None:
    document = parsed_document(
        ParsedBlock("one two", section_path="A"),
        ParsedBlock("three four", section_path="A"),
        ParsedBlock("five six", section_path="B"),
    )
    chunker = StructureAwareChunker(
        WhitespaceTokenCounter(),
        ChunkingConfig(target_tokens=8, max_tokens=10, overlap_tokens=2),
    )

    chunks = chunker.chunk(document)

    assert len(chunks) == 2
    assert chunks[0].section_path == "A"
    assert chunks[0].content == "A\n\none two\n\nthree four"
    assert chunks[1].section_path == "B"
    assert chunks[1].content.startswith("B\n\n")


def test_section_path_is_part_of_every_embedding_chunk_and_token_budget() -> None:
    document = parsed_document(
        ParsedBlock("one two three four five six seven eight", section_path="Leave Rules")
    )
    chunker = StructureAwareChunker(
        WhitespaceTokenCounter(),
        ChunkingConfig(target_tokens=4, max_tokens=6, overlap_tokens=1),
    )

    chunks = chunker.chunk(document)

    assert len(chunks) > 1
    assert all(chunk.content.startswith("Leave Rules\n\n") for chunk in chunks)
    assert all(chunk.token_count <= 6 for chunk in chunks)


def test_section_path_that_consumes_budget_is_rejected() -> None:
    document = parsed_document(ParsedBlock("x", section_path="1234567890"))
    chunker = StructureAwareChunker(
        CharacterTokenCounter(),
        ChunkingConfig(target_tokens=8, max_tokens=10, overlap_tokens=1),
    )

    with pytest.raises(ValidationError, match="section path"):
        chunker.chunk(document)


def test_unbroken_content_is_force_split_below_the_hard_limit() -> None:
    document = parsed_document(ParsedBlock("字" * 1_000, page_number=1))
    chunker = StructureAwareChunker(CharacterTokenCounter())

    chunks = chunker.chunk(document)

    assert len(chunks) >= 3
    assert all(chunk.token_count <= 450 for chunk in chunks)
    assert all(chunk.token_count <= 512 for chunk in chunks)
    assert all(chunk.page_number == 1 for chunk in chunks)


@pytest.mark.parametrize(
    "config",
    [
        ChunkingConfig(target_tokens=350, max_tokens=450, overlap_tokens=50),
        ChunkingConfig(target_tokens=1, max_tokens=1, overlap_tokens=0, model_max_tokens=1),
    ],
)
def test_valid_chunking_configurations(config: ChunkingConfig) -> None:
    assert config.max_tokens <= 512


def test_invalid_chunking_configurations_are_rejected() -> None:
    with pytest.raises(ValueError, match="never exceed 512"):
        ChunkingConfig(target_tokens=500, max_tokens=513, overlap_tokens=20)
    with pytest.raises(ValueError, match="smaller than target"):
        ChunkingConfig(target_tokens=10, max_tokens=10, overlap_tokens=10)
    with pytest.raises(ValueError, match="model_max_tokens"):
        ChunkingConfig(
            target_tokens=350,
            max_tokens=450,
            overlap_tokens=50,
            model_max_tokens=400,
        )
