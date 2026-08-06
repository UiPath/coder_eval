"""Agent-visible plugin bundles: file-level allowlist staging + digest verification.

Tasks hand agents a local plugin source (``agent.plugins: [{type: local, path:
"$SKILLS_REPO_PATH"}]``). Expanded raw, that path points at the entire skills
checkout — which carries its own answer key (``RESOLUTION.md`` reference
answers, ``check_*.py`` grader scripts, ``tests/`` fixtures and golden
outputs). This module stages a sanitized copy — the *bundle* — and the
orchestrator rewrites ``plugins[].path`` to it, so the agent never sees the
raw checkout. Grading is unaffected: ``run_command`` criteria and the sandbox
environment keep resolving ``$SKILLS_REPO_PATH`` to the raw path.

Threat model: task authors are TRUSTED; the agent under evaluation is the sole
adversary. The skills repo legitimately stores hundreds of ``RESOLUTION.md`` /
``check_*.py`` files alongside the skill docs the agent must read, so the
checks below are (a) the projection that keeps that material out of the
agent's view and (b) loud authoring guardrails that catch mistakes (grading
material misfiled under ``skills/``, staging drift) — not defenses against a
hostile task author.

Three layers, each failing CLOSED (a violation raises
:class:`PluginBundleError`; nothing ever falls back to the raw path):

1. **Subtree allowlist** — only Claude Code's plugin-discovery subtrees
   (``PLUGIN_AGENT_ALLOWED_SUBDIRS``) are considered. An allowlist, not a
   denylist, so a new answer-bearing top-level directory is excluded by
   default. Mirrors PR #85's docker-side projection; this module is the
   canonical, driver-independent home so the two cannot diverge.
2. **File-level manifest** — every staged file is declared (bundle-relative
   path -> sha256) at build time. A recognizable grading artifact *inside* an
   allowed subtree (e.g. ``skills/x/RESOLUTION.md``) fails the build loudly
   instead of shipping; a symlink whose target escapes the source root fails
   the build; in-root symlinks are copied verbatim (never followed —
   loop-proof against self-referential marketplace symlinks).
3. **Runtime digest verification** — before every agent start the staged
   bundle is re-hashed against its recorded manifest digest. Any drift
   (tampered, added, or missing file) aborts the run with a clear setup
   error.

Bundles are built ONCE per resolved source path per process and cached
(thread-safe): the suite runs hundreds of tasks against one skills checkout,
and a per-task recursive copy would be a serious regression. Verification
(hashing only) runs per task.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import logging
import os
import shutil
import tempfile
import threading
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from coder_eval.utils import process_plugins


logger = logging.getLogger(__name__)

# Claude Code's plugin discovery surface. Only these top-level subdirs of a
# plugin root are staged for the agent; grader / reference / fixture trees are
# never in this set. Keep in sync with (and canonical over) the docker
# projection in PR #85's plugin_projection module.
PLUGIN_AGENT_ALLOWED_SUBDIRS = frozenset({"skills", "commands", "agents", ".claude-plugin", "hooks"})

# Grading material recognizable by NAME even inside an allowed subtree.
# Matched case-insensitively; a hit FAILS the build (loud, not a silent skip)
# so answer-key material can never ship by being filed under skills/.
HIDDEN_MATERIAL_FILE_PATTERNS: tuple[str, ...] = ("resolution.md", "check_*.py")
HIDDEN_MATERIAL_DIR_NAMES: frozenset[str] = frozenset({"tests"})

MANIFEST_SUFFIX = ".manifest.json"


class PluginBundleError(Exception):
    """A plugin bundle could not be built or verified; the run must not start."""


@dataclass(frozen=True)
class BundleManifest:
    """The file-level allowlist for one staged bundle.

    ``files`` maps bundle-relative POSIX paths to sha256 hex digests of file
    content; ``symlinks`` maps bundle-relative POSIX paths to their raw link
    targets. ``digest`` is the sha256 of the canonical JSON of both maps —
    the single value runtime verification compares against.
    """

    source: str
    files: dict[str, str]
    symlinks: dict[str, str]
    digest: str


def _compute_digest(files: dict[str, str], symlinks: dict[str, str]) -> str:
    canonical = json.dumps({"files": files, "symlinks": symlinks}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _hash_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _is_hidden_material(name: str, *, is_dir: bool) -> bool:
    lowered = name.lower()
    if is_dir:
        return lowered in HIDDEN_MATERIAL_DIR_NAMES
    return any(fnmatchcase(lowered, pattern) for pattern in HIDDEN_MATERIAL_FILE_PATTERNS)


def _check_symlink_in_root(link: Path, source_root: Path) -> str:
    """Return the link's raw target if it resolves inside ``source_root``, else raise.

    Resolution uses ``os.path.realpath`` (non-strict): a broken in-root link is
    fine (copied verbatim, dangles harmlessly in the bundle) and a self-
    referential loop resolves to an in-root path rather than recursing — the
    walk never follows links, so loops cannot recurse either.
    """
    target = os.readlink(link)
    resolved = Path(os.path.realpath(link))
    root = Path(os.path.realpath(source_root))
    if not resolved.is_relative_to(root):
        raise PluginBundleError(
            f"Plugin bundle build failed: symlink {link} -> {target!r} escapes the source root {source_root}"
        )
    return target


def build_manifest(source: Path) -> BundleManifest:
    """Walk ``source``'s allowed subtrees and declare every agent-visible file.

    Raises :class:`PluginBundleError` on a symlink escaping the source root or
    on hidden grading material (``HIDDEN_MATERIAL_FILE_PATTERNS`` /
    ``HIDDEN_MATERIAL_DIR_NAMES``) inside an allowed subtree.
    """
    files: dict[str, str] = {}
    symlinks: dict[str, str] = {}

    def record(path: Path) -> None:
        rel = path.relative_to(source).as_posix()
        if path.is_symlink():
            symlinks[rel] = _check_symlink_in_root(path, source)
        elif _is_hidden_material(path.name, is_dir=False):
            raise PluginBundleError(
                f"Plugin bundle build failed: hidden grading material inside an allowed subtree: {rel} "
                + f"(patterns: {', '.join(HIDDEN_MATERIAL_FILE_PATTERNS)})"
            )
        else:
            files[rel] = _hash_file(path)

    for name in sorted(PLUGIN_AGENT_ALLOWED_SUBDIRS):
        top = source / name
        if top.is_symlink() or top.is_file():
            # .claude-plugin can be a file (plugin manifest) in some layouts;
            # a symlinked top entry is recorded verbatim, never walked.
            record(top)
            continue
        if not top.is_dir():
            continue
        for root_str, dirnames, filenames in os.walk(top):  # followlinks=False: never descend through links
            root = Path(root_str)
            for dirname in sorted(dirnames):
                child = root / dirname
                if _is_hidden_material(dirname, is_dir=True):
                    rel = child.relative_to(source).as_posix()
                    raise PluginBundleError(
                        f"Plugin bundle build failed: hidden grading directory inside an allowed subtree: {rel}/"
                    )
                if child.is_symlink():
                    # Listed but never descended into by os.walk; record verbatim.
                    symlinks[child.relative_to(source).as_posix()] = _check_symlink_in_root(child, source)
            for filename in sorted(filenames):
                record(root / filename)

    return BundleManifest(
        source=str(source),
        files=files,
        symlinks=symlinks,
        digest=_compute_digest(files, symlinks),
    )


def manifest_path_for(bundle_dir: Path) -> Path:
    """The manifest lives NEXT TO the bundle dir, never inside it — the bundle
    contains only projected plugin content."""
    return bundle_dir.with_name(bundle_dir.name + MANIFEST_SUFFIX)


def stage_bundle(source: Path, bundle_dir: Path) -> BundleManifest:
    """Build the manifest for ``source``, copy the declared files into
    ``bundle_dir``, record the manifest + digest, and self-verify.

    Symlinks are recreated verbatim (not followed). The post-copy
    :func:`verify_bundle` self-check guarantees the staged tree matches the
    manifest exactly — an undeclared file in the bundle fails the build.
    """
    manifest = build_manifest(source)
    bundle_dir.mkdir(parents=True, exist_ok=True)
    try:
        for rel in manifest.files:
            src = source / rel
            dst = bundle_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        for rel, target in manifest.symlinks.items():
            src = source / rel
            dst = bundle_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(target, dst, target_is_directory=src.is_dir())
    except OSError as exc:
        raise PluginBundleError(f"Plugin bundle build failed copying {source} -> {bundle_dir}: {exc}") from exc

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
    verify_bundle(bundle_dir)  # post-build self-check: staged tree == manifest, digest intact
    return manifest


def verify_bundle(bundle_dir: Path) -> BundleManifest:
    """Verify the staged bundle matches its recorded manifest digest; fail closed.

    Re-hashes every file in the bundle and cross-checks BOTH directions
    against the manifest: a tampered file, an undeclared (added) file, a
    missing declared file, a changed symlink target, or a manifest whose own
    digest does not match its maps all raise :class:`PluginBundleError`.
    """
    mpath = manifest_path_for(bundle_dir)
    try:
        raw = json.loads(mpath.read_text(encoding="utf-8"))
        manifest = BundleManifest(
            source=raw["source"], files=raw["files"], symlinks=raw["symlinks"], digest=raw["digest"]
        )
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise PluginBundleError(f"Plugin bundle manifest unreadable at {mpath}: {exc}") from exc

    if _compute_digest(manifest.files, manifest.symlinks) != manifest.digest:
        raise PluginBundleError(f"Plugin bundle manifest at {mpath} fails its own digest; refusing to run")

    actual_files: dict[str, str] = {}
    actual_symlinks: dict[str, str] = {}
    for root_str, dirnames, filenames in os.walk(bundle_dir):  # followlinks=False
        root = Path(root_str)
        for dirname in list(dirnames):
            child = root / dirname
            if child.is_symlink():
                actual_symlinks[child.relative_to(bundle_dir).as_posix()] = os.readlink(child)
        for filename in filenames:
            child = root / filename
            rel = child.relative_to(bundle_dir).as_posix()
            if child.is_symlink():
                actual_symlinks[rel] = os.readlink(child)
            else:
                actual_files[rel] = _hash_file(child)

    if actual_files != manifest.files or actual_symlinks != manifest.symlinks:
        missing = sorted(set(manifest.files) - set(actual_files))[:5]
        undeclared = sorted(set(actual_files) - set(manifest.files))[:5]
        changed = sorted(k for k in set(actual_files) & set(manifest.files) if actual_files[k] != manifest.files[k])[:5]
        link_drift = actual_symlinks != manifest.symlinks
        raise PluginBundleError(
            f"Plugin bundle at {bundle_dir} does not match its manifest digest "
            + f"(missing={missing}, undeclared={undeclared}, changed={changed}, symlink_drift={link_drift}); "
            + "the bundle drifted since staging — aborting instead of falling back to the raw plugin path"
        )
    return manifest


# --- once-per-run staging cache -------------------------------------------
#
# One bundle per resolved source path per process. run_batch executes tasks
# concurrently (asyncio + to_thread), so the build is serialized under a
# process-wide lock; cache hits return the already-staged dir and re-verify.
_STAGE_LOCK = threading.Lock()
_BUNDLE_CACHE: dict[str, Path] = {}
_STAGING_ROOT: Path | None = None


def _staging_root() -> Path:
    global _STAGING_ROOT
    if _STAGING_ROOT is None:
        root = Path(tempfile.mkdtemp(prefix="coder-eval-plugin-bundles-"))
        atexit.register(shutil.rmtree, root, ignore_errors=True)
        _STAGING_ROOT = root
    return _STAGING_ROOT


def _get_or_build_bundle(source: Path, log: logging.Logger | logging.LoggerAdapter[Any]) -> Path:
    key = str(source)
    with _STAGE_LOCK:
        cached = _BUNDLE_CACHE.get(key)
        if cached is not None:
            return cached
        bundle_dir = _staging_root() / f"{source.name}-{hashlib.sha256(key.encode('utf-8')).hexdigest()[:8]}"
        manifest = stage_bundle(source, bundle_dir)
        if not manifest.files and not manifest.symlinks:
            log.warning(
                "Plugin source %s contains none of the allowed subtrees %s; the agent sees an EMPTY plugin "
                + "bundle (nothing leaks, but no skills will be discovered — check the plugin path)",
                source,
                sorted(PLUGIN_AGENT_ALLOWED_SUBDIRS),
            )
        else:
            log.info(
                "Staged agent plugin bundle %s -> %s (%d files, %d symlinks, digest %s)",
                source,
                bundle_dir,
                len(manifest.files),
                len(manifest.symlinks),
                manifest.digest[:12],
            )
        _BUNDLE_CACHE[key] = bundle_dir
        return bundle_dir


def stage_agent_plugins(
    plugins: list[dict[str, Any]],
    *,
    log: logging.Logger | logging.LoggerAdapter[Any] = logger,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Rewrite local plugin entries to point at verified agent-visible bundles.

    Env vars in each ``path`` are expanded first (same semantics/warnings as
    the agents' own :func:`coder_eval.utils.process_plugins` pass — which then
    no-ops on the already-absolute bundle path). Entries whose path does not
    resolve to an existing directory pass through unchanged; the agents
    already warn loudly about those. Every returned bundle — cache hit or
    fresh build — is digest-verified here, immediately before the agent
    starts.

    Returns ``(staged_plugins, digests)`` where ``digests`` maps each raw
    source path to its manifest digest (recorded on the run for audit).
    """
    staged: list[dict[str, Any]] = []
    digests: dict[str, str] = {}
    for plugin in process_plugins(plugins, log=log):
        path = plugin.get("path")
        if not path or not Path(path).is_dir():
            staged.append(plugin)
            continue
        source = Path(path)
        bundle_dir = _get_or_build_bundle(source, log)
        manifest = verify_bundle(bundle_dir)
        digests[str(source)] = manifest.digest
        staged.append({**plugin, "path": str(bundle_dir)})
    return staged, digests
