from typing import Any, Literal

from pydantic import BaseModel, Field


class ParsedBlock(BaseModel):
    kind: Literal["paragraph", "heading", "table", "sheet", "record", "ocr"]
    text: str = Field(min_length=1, max_length=200000)
    location: dict[str, Any] = Field(default_factory=dict)


class ParsedDocument(BaseModel):
    filename: str
    source_type: str
    title: str
    sha256: str
    parser_version: str = "etl-1.2"
    blocks: list[ParsedBlock]
    warnings: list[str] = Field(default_factory=list)

    @property
    def character_count(self) -> int:
        return sum(len(item.text) for item in self.blocks)


class Chunk(BaseModel):
    index: int
    text: str = Field(min_length=1, max_length=2000)
    source: dict[str, Any]
