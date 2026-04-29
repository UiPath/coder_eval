"""Template source models for sandbox initialization."""

from __future__ import annotations

import os
from abc import ABC
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class StarterFile(BaseModel):
    """A file to create in the sandbox before agent starts."""

    path: str = Field(description="Relative path in sandbox (e.g., 'src/main.py')")
    content: str = Field(description="File content")


class BaseTemplateSource(BaseModel, ABC):
    """Base class for template sources - defines the discriminated union.

    Note: No type field here - each subclass defines its own Literal type.
    """

    pass


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


class StarterFilesSource(BaseTemplateSource):
    """Create inline files from YAML definitions."""

    type: Literal["starter_files"] = "starter_files"
    files: list[StarterFile] = Field(description="List of files to create")


class RepoSource(BaseTemplateSource):
    """Clone files from a git repository."""

    type: Literal["repo"] = "repo"
    url: str = Field(description="Git repository URL")
    commit: str | None = Field(default=None, description="Specific commit SHA to checkout")


# Discriminated union of template sources
TemplateSource = TemplateDirSource | StarterFilesSource | RepoSource
