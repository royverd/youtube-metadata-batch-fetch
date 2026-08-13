"""
schema.py

The output shape, defined once. Both providers derive their schema from this:
Anthropic takes the generated JSON Schema, Gemini takes the Pydantic class
directly, and both responses get validated back through it. Two hand-written
schemas would drift the first time a field is added; this cannot.

Field semantics live in prompts/describe_v1.txt, not here - descriptions in
this file would be a second place to keep the wording in sync.

Deps: pydantic (already required by anthropic).
"""

from typing import List, Literal

from pydantic import BaseModel

Level = Literal[1, 2, 3, 4, 5]

AXES = ["depth", "breadth", "rigor", "sourcing", "prerequisites", "density"]


class Axes(BaseModel):
    model_config = {"extra": "forbid"}
    depth: Level
    breadth: Level
    rigor: Level
    sourcing: Level
    prerequisites: Level
    density: Level


class Evidence(BaseModel):
    """One verbatim transcript quote per axis. These are the whole verification
    story - a reader checks the quote against the video, which is possible in a
    way that checking a judgment never was."""

    model_config = {"extra": "forbid"}
    depth: str
    breadth: str
    rigor: str
    sourcing: str
    prerequisites: str
    density: str


class VideoDescription(BaseModel):
    model_config = {"extra": "forbid"}

    subject: str
    format: str
    description: str
    audience_for: str
    audience_not_for: str
    length_verdict: str
    padding_fraction: float
    axes: Axes
    evidence: Evidence
    on_screen: Literal["low", "medium", "high"]
    on_screen_note: str
    keywords: List[str]
    undetermined: str


def json_schema():
    """JSON Schema for providers that want the schema rather than the class."""
    return VideoDescription.model_json_schema()
