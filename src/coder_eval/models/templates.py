"""Template source models for sandbox initialization."""

from __future__ import annotations

import os
from abc import ABC
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StarterFile(BaseModel):
    """A file to create in the sandbox before agent starts."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(description="Relative path in sandbox (e.g., 'src/main.py')")
    content: str = Field(description="File content")


class BaseTemplateSource(BaseModel, ABC):
    """Base class for template sources - defines the discriminated union.

    Note: No type field here - each subclass defines its own Literal type.
    """

    model_config = ConfigDict(extra="forbid")

    def model_post_init(self, context: Any, /) -> None:
        # Pin the discriminator tag into model_fields_set so it survives
        # model_dump(exclude_unset=True) → model_validate() round-trips (the
        # sandbox layer merge dumps with exclude_unset) even for
        # directly-constructed sources, where the tag comes from the Literal
        # default rather than the caller.
        self.__pydantic_fields_set__.add("type")


class TemplateDirSource(BaseTemplateSource):
    """Copy files from a local directory into the sandbox."""

    type: Literal["template_dir"] = "template_dir"
    path: str = Field(description="Path to template directory (relative to task YAML or absolute)")
    mount_point: str = Field(
        default=".",
        description=(
            "Destination subdirectory inside the sandbox where the template contents are copied. "
            "Defaults to '.' (sandbox root). Must be a relative path that stays within the sandbox."
        ),
    )
    include_patterns: list[str] = Field(
        default_factory=list,
        description=(
            "Template-relative glob patterns to copy even when the path matches a default sandbox "
            "ignore pattern. Author-controlled — re-including sensitive directories such as `.git` "
            "or `.env` is possible, so review patterns when writing tasks. `*` does not stop at `/`, "
            "see `_matches_template_include_pattern`."
        ),
    )

    @field_validator("mount_point")
    @classmethod
    def _validate_mount_point(cls, v: str) -> str:
        if not v:
            raise ValueError("mount_point must not be empty")
        if os.path.isabs(v) or v.startswith(("/", "\\")):
            raise ValueError(f"mount_point must be a relative path, got: {v!r}")
        parts = v.replace("\\", "/").split("/")
        if any(p == ".." for p in parts):
            raise ValueError(f"mount_point must not contain '..' segments, got: {v!r}")
        return v

    @field_validator("include_patterns")
    @classmethod
    def _validate_include_patterns(cls, v: list[str]) -> list[str]:
        for pattern in v:
            if not pattern:
                raise ValueError("include_patterns entries must not be empty")
            if os.path.isabs(pattern) or pattern.startswith(("/", "\\")):
                raise ValueError(f"include_patterns entries must be relative, got: {pattern!r}")
            parts = pattern.replace("\\", "/").split("/")
            if any(p == ".." for p in parts):
                raise ValueError(f"include_patterns entries must not contain '..' segments, got: {pattern!r}")
        return v


class StarterFilesSource(BaseTemplateSource):
    """Create inline files from YAML definitions."""

    type: Literal["starter_files"] = "starter_files"
    files: list[StarterFile] = Field(description="List of files to create")


class RepoSource(BaseTemplateSource):
    """Clone files from a git repository."""

    type: Literal["repo"] = "repo"
    url: str = Field(description="Git repository URL")
    commit: str | None = Field(default=None, description="Specific commit SHA to checkout")


# Discriminated union of template sources. The `type` tag is REQUIRED in
# dict/YAML input — a missing or unknown tag raises one crisp discriminator
# error instead of smart-union coercion. Per-variant Literal defaults remain
# for direct construction and serialization.
TemplateSource = Annotated[
    TemplateDirSource | StarterFilesSource | RepoSource,
    Field(discriminator="type"),
]
