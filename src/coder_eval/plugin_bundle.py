"""Build a manifest-verified, agent-visible projection of a local plugin.

Local skill repositories commonly contain both public instructions and hidden
graders, references, fixtures, or resolution notes.  Docker evaluations must
never mount that complete tree at an agent-readable path.  This module copies
only the plugin discovery surface and records every copied file in a digest
manifest.  Any violation fails closed; callers must not fall back to the raw
source path.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path


PLUGIN_AGENT_ALLOWED_SUBDIRS = frozenset({"skills", "commands", "agents", ".claude-plugin", "hooks"})
HIDDEN_MATERIAL_FILE_PATTERNS: tuple[str, ...] = ("resolution.md", "check_*.py")
MANIFEST_SUFFIX = ".manifest.json"


class PluginBundleError(RuntimeError):
    """The sanitized plugin bundle could not be built or verified safely."""


@dataclass(frozen=True)
class BundleManifest:
    """Content inventory for one sanitized plugin bundle."""

    source: str
    files: dict[str, str]
    symlinks: dict[str, str]
    digest: str


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_digest(files: dict[str, str], symlinks: dict[str, str]) -> str:
    payload = json.dumps({"files": files, "symlinks": symlinks}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _is_hidden_material(path: Path) -> bool:
    name = path.name.lower()
    return any(fnmatchcase(name, pattern) for pattern in HIDDEN_MATERIAL_FILE_PATTERNS)


def _validate_symlink(link: Path, source_root: Path) -> str:
    """Return a safe relative link target or raise.

    Absolute links are rejected even when they currently resolve inside the
    source tree: recreating one in the bundle would point back to the raw host
    checkout.  Relative links must be unbroken and resolve to either the source
    root or one of the explicitly included top-level subtrees.  Recreating the
    same relative target therefore cannot make excluded source content visible.
    """

    raw_target = os.readlink(link)
    if Path(raw_target).is_absolute():
        raise PluginBundleError(f"absolute plugin symlink is not allowed: {link} -> {raw_target!r}")

    try:
        root = source_root.resolve(strict=True)
        resolved = link.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise PluginBundleError(f"broken or looping plugin symlink is not allowed: {link} -> {raw_target!r}") from exc

    if resolved != root and root not in resolved.parents:
        raise PluginBundleError(f"plugin symlink escapes its source root: {link} -> {raw_target!r}")

    if resolved != root:
        relative_target = resolved.relative_to(root)
        if not relative_target.parts or relative_target.parts[0] not in PLUGIN_AGENT_ALLOWED_SUBDIRS:
            raise PluginBundleError(
                f"plugin symlink targets excluded content: {link} -> {raw_target!r} ({relative_target.as_posix()})"
            )
    return raw_target


def build_manifest(source: Path) -> BundleManifest:
    """Inventory the allowed plugin projection and fail on hidden material."""

    try:
        source = source.resolve(strict=True)
    except OSError as exc:
        raise PluginBundleError(f"plugin source does not exist: {source}") from exc
    if not source.is_dir():
        raise PluginBundleError(f"plugin source is not a directory: {source}")

    files: dict[str, str] = {}
    symlinks: dict[str, str] = {}

    def record(path: Path) -> None:
        relative = path.relative_to(source).as_posix()
        if path.is_symlink():
            symlinks[relative] = _validate_symlink(path, source)
            return
        if _is_hidden_material(path):
            patterns = ", ".join(HIDDEN_MATERIAL_FILE_PATTERNS)
            raise PluginBundleError(
                f"hidden grading material appears inside an agent-visible plugin subtree: {relative} "
                + f"(forbidden patterns: {patterns})"
            )
        if path.is_file():
            files[relative] = _hash_file(path)

    for name in sorted(PLUGIN_AGENT_ALLOWED_SUBDIRS):
        top = source / name
        if top.is_symlink() or top.is_file():
            record(top)
            continue
        if not top.is_dir():
            continue
        for root_name, dirnames, filenames in os.walk(top, followlinks=False):
            root = Path(root_name)
            for dirname in sorted(dirnames):
                child = root / dirname
                if child.is_symlink():
                    record(child)
            for filename in sorted(filenames):
                record(root / filename)

    return BundleManifest(
        source=str(source),
        files=dict(sorted(files.items())),
        symlinks=dict(sorted(symlinks.items())),
        digest=_manifest_digest(files, symlinks),
    )


def manifest_path_for(bundle_dir: Path) -> Path:
    """Keep the inventory beside, rather than inside, the public bundle."""

    return bundle_dir.with_name(bundle_dir.name + MANIFEST_SUFFIX)


def stage_bundle(source: Path, bundle_dir: Path) -> BundleManifest:
    """Create and self-verify a sanitized plugin bundle.

    ``bundle_dir`` must not already contain data.  This prevents a prior task or
    failed attempt from leaving undeclared files in a newly staged projection.
    """

    if bundle_dir.exists() and any(bundle_dir.iterdir()):
        raise PluginBundleError(f"plugin bundle destination is not empty: {bundle_dir}")

    manifest = build_manifest(source)
    bundle_dir.mkdir(parents=True, exist_ok=True)
    source_root = Path(manifest.source)
    try:
        for relative in manifest.files:
            destination = bundle_dir / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_root / relative, destination)
        for relative, target in manifest.symlinks.items():
            original = source_root / relative
            destination = bundle_dir / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(target, destination, target_is_directory=original.is_dir())
    except OSError as exc:
        raise PluginBundleError(f"failed to copy sanitized plugin bundle {source_root}: {exc}") from exc

    # The bundle is disposable public staging. Normalize away restrictive
    # source modes so the unrelated agent UID can read it through a read-only
    # bind mount; never apply these changes to ``source_root``.
    try:
        bundle_dir.chmod(0o555)
        for root_name, dirnames, filenames in os.walk(bundle_dir, followlinks=False):
            root = Path(root_name)
            for dirname in dirnames:
                child = root / dirname
                if not child.is_symlink():
                    child.chmod(0o555)
            for filename in filenames:
                child = root / filename
                if not child.is_symlink():
                    child.chmod(0o444)
    except OSError as exc:
        raise PluginBundleError(f"failed to normalize public bundle permissions at {bundle_dir}: {exc}") from exc

    manifest_path_for(bundle_dir).write_text(
        json.dumps(
            {
                "source": manifest.source,
                "files": manifest.files,
                "symlinks": manifest.symlinks,
                "digest": manifest.digest,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return verify_bundle(bundle_dir)


def verify_bundle(bundle_dir: Path) -> BundleManifest:
    """Verify manifest integrity and both directions of the staged inventory."""

    manifest_path = manifest_path_for(bundle_dir)
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest = BundleManifest(
            source=str(raw["source"]),
            files={str(k): str(v) for k, v in raw["files"].items()},
            symlinks={str(k): str(v) for k, v in raw["symlinks"].items()},
            digest=str(raw["digest"]),
        )
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise PluginBundleError(f"plugin bundle manifest is unreadable: {manifest_path}: {exc}") from exc

    if _manifest_digest(manifest.files, manifest.symlinks) != manifest.digest:
        raise PluginBundleError(f"plugin bundle manifest digest is invalid: {manifest_path}")

    actual_files: dict[str, str] = {}
    actual_symlinks: dict[str, str] = {}
    for root_name, dirnames, filenames in os.walk(bundle_dir, followlinks=False):
        root = Path(root_name)
        for dirname in dirnames:
            child = root / dirname
            if child.is_symlink():
                actual_symlinks[child.relative_to(bundle_dir).as_posix()] = os.readlink(child)
        for filename in filenames:
            child = root / filename
            relative = child.relative_to(bundle_dir).as_posix()
            if child.is_symlink():
                actual_symlinks[relative] = os.readlink(child)
            else:
                actual_files[relative] = _hash_file(child)

    actual_files = dict(sorted(actual_files.items()))
    actual_symlinks = dict(sorted(actual_symlinks.items()))
    if actual_files != manifest.files or actual_symlinks != manifest.symlinks:
        declared_paths = set(manifest.files) | set(manifest.symlinks)
        actual_paths = set(actual_files) | set(actual_symlinks)
        missing = sorted(declared_paths - actual_paths)[:5]
        added = sorted(actual_paths - declared_paths)[:5]
        changed = sorted(
            key for key in set(actual_files) & set(manifest.files) if actual_files[key] != manifest.files[key]
        )[:5]
        raise PluginBundleError(
            f"plugin bundle differs from its manifest: {bundle_dir} "
            + f"(missing={missing}, added={added}, changed={changed}, "
            + f"symlink_drift={actual_symlinks != manifest.symlinks})"
        )
    return manifest
