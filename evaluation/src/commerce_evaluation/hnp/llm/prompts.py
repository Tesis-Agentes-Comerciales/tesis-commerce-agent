"""Versioned prompt files. Name, version and SHA-256 of the exact bytes go into every trace.

A prompt change is a new file (``validator-v2.md``), never an edit of a published version.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from commerce_agent.governance.canonical import sha256_digest

PROMPT_DIRECTORY = Path(__file__).with_name("prompts")
PROMPT_NAME = re.compile(r"^(?P<role>[a-z]+)-(?P<version>v[0-9]+)$")


@dataclass(frozen=True)
class PromptSpec:
    name: str
    role: str
    version: str
    sha256: str
    text: str


def load_prompt(name: str, directory: Path = PROMPT_DIRECTORY) -> PromptSpec:
    match = PROMPT_NAME.fullmatch(name)
    if match is None:
        raise ValueError("Invalid prompt name")
    data = (directory / f"{name}.md").read_bytes()
    # .gitattributes pins LF; a CRLF checkout would silently change the recorded hash.
    if b"\r" in data:
        raise ValueError("Prompt files must use LF line endings")
    text = data.decode("utf-8")
    if not text.strip():
        raise ValueError("Empty prompt")
    return PromptSpec(
        name=name,
        role=match["role"],
        version=match["version"],
        sha256=sha256_digest(data),
        text=text,
    )
