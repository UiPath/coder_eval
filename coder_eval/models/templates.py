"""Template source models for sandbox initialization."""

from __future__ import annotations

from abc import ABC
from typing import Literal

from pydantic import BaseModel, Field


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
