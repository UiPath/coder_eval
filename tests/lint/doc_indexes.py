"""CE028 — the flat doc-index surfaces are generated from the mkdocs nav.

The docs overhaul's root cause was doc/code drift; the index surfaces
(``README.md``'s Documentation table, ``docs/index.md``'s "Where to go next"
table, and the ``## Docs`` / ``## Tutorials`` sections of ``docs/llms.txt``) drift
the same way — a page is added to the nav and forgotten in the three flat lists,
or a page is deleted and left dangling in them. This module makes ``nav`` (plus
the ``extra.docs_index`` blurb map) in ``mkdocs.yml`` the single source of truth:
``write()`` renders all three surfaces from it, ``make docs-indexes`` calls
``write()``, and CE028 (``check()``) re-renders and diffs against disk. There is
deliberately **no ``--check`` mode and no arg parser** — CE028 *is* the checker; a
second entry point would be untested duplication.

CE028 also enforces the invariants the render depends on: every nav page has a
blurb and every blurb has a nav page (bijection, tutorial leaves exempted), every
published ``docs/*.md`` is in the nav (the check that would have caught this whole
overhaul's bug class), and the hand-written ``docs/tutorials/README.md`` table
stays in parity with the nav's tutorial pages (that table is **checked, not
generated** — generating it would need a second per-page field).

Like CE027/CE029/CE030 this is not a ``BaseRule`` in the AST runner; it reasons
over Markdown/YAML and is wired as
``tests/test_custom_lint.py::TestCE028DocIndexParity``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import NamedTuple

import yaml

from tests.lint.generated import diff_all, write_all


# --- Markers (already present in the target files; write() fills between them) ---
_START = "<!-- docs-index:start -->"
_END = "<!-- docs-index:end -->"
_START_DOCS = "<!-- docs-index:start:docs -->"
_END_DOCS = "<!-- docs-index:end:docs -->"
_START_TUT = "<!-- docs-index:start:tutorials -->"
_END_TUT = "<!-- docs-index:end:tutorials -->"

_SITE = "https://coder-eval.com"
_NAV_EXCLUDE_FROM_NAV_CHECK = {"IDEAS.md"}  # published outside the nav on purpose
_TUTORIALS_GROUP = "Tutorials"
_TUTORIAL_README = "tutorials/README.md"


class NavPage(NamedTuple):
    """One resolved nav leaf, in nav order.

    ``group`` is the parent group label (e.g. ``"Advanced"``) or None for a
    top-level page; ``doc`` is the docs-root-relative path (``agents/CODEX.md``).
    """

    group: str | None
    label: str
    doc: str


def _is_tutorial_leaf(doc: str) -> bool:
    """A numbered tutorial page (``tutorials/01-...md``) — no blurb, own llms section."""
    return doc.startswith("tutorials/0")


def _load_mkdocs(mkdocs_path: Path) -> dict:
    """Parse ``mkdocs.yml`` with a private ``SafeLoader`` subclass.

    ``mkdocs.yml`` carries the custom tag ``!ENV [CI, false]``, which
    ``yaml.safe_load`` rejects. A private ``SafeLoader`` **subclass** tolerates
    local ``!``-prefixed tags without touching global YAML state — mutating the
    shared ``SafeLoader`` would disable unknown-tag rejection for every other
    ``yaml.safe_load`` in the same pytest worker under ``-n auto``. ``!!python/...``
    tags expand to the ``tag:yaml.org,2002:`` handle, which this multi-constructor
    does NOT match, so the subclass still rejects them — this stays
    safe_load-equivalent for object construction (proven in the CE028 tests).
    """

    class _NavLoader(yaml.SafeLoader):
        """Private loader: tolerates mkdocs custom tags (!ENV) without touching global state."""

    _NavLoader.add_multi_constructor("!", lambda loader, suffix, node: None)
    # yaml.load with a private SafeLoader subclass — safe_load-equivalent (see above), not unsafe_load.
    return yaml.load(mkdocs_path.read_text(encoding="utf-8"), Loader=_NavLoader)


def load_nav(mkdocs_path: Path) -> list[NavPage]:
    """Parse ``nav:`` into an ordered list of ``NavPage`` (nav order preserved)."""
    data = _load_mkdocs(mkdocs_path)
    pages: list[NavPage] = []
    for entry in data["nav"]:
        ((label, value),) = entry.items()
        if isinstance(value, str):
            pages.append(NavPage(group=None, label=label, doc=value))
        elif isinstance(value, list):
            for sub in value:
                ((sub_label, sub_doc),) = sub.items()
                pages.append(NavPage(group=label, label=sub_label, doc=sub_doc))
    return pages


def load_blurbs(mkdocs_path: Path) -> dict[str, str]:
    """The ``extra.docs_index`` map: docs-root-relative path -> one-line blurb."""
    data = _load_mkdocs(mkdocs_path)
    return dict(data.get("extra", {}).get("docs_index", {}))


def route_for(doc: str) -> str:
    """The published site route for a docs-root-relative path.

    ``index.md`` and any ``README.md`` map to their directory landing route;
    other segments are kebab-cased (``_``/space -> ``-``, lowered). Examples:
    ``index.md`` -> ``/docs``; ``USER_GUIDE.md`` -> ``/docs/user-guide``;
    ``agents/CLAUDE_CODE.md`` -> ``/docs/agents/claude-code``;
    ``tutorials/README.md`` -> ``/docs/tutorials``.
    """
    stem = doc[:-3] if doc.endswith(".md") else doc
    parts = stem.split("/")
    if parts[-1] in ("index", "README"):
        parts = parts[:-1]
    suffix = "/".join(seg.replace("_", "-").replace(" ", "-").lower() for seg in parts)
    return "/docs" + (f"/{suffix}" if suffix else "")


def _table_rows(nav: list[NavPage]) -> list[tuple[str, str]]:
    """(label, doc) rows for the README/index tables: nav order, ``index.md``
    skipped, the whole Tutorials group collapsed to one ``Tutorials`` row."""
    rows: list[tuple[str, str]] = []
    tutorials_done = False
    for page in nav:
        if page.doc == "index.md":
            continue
        if page.group == _TUTORIALS_GROUP:
            if not tutorials_done:
                rows.append((_TUTORIALS_GROUP, _TUTORIAL_README))
                tutorials_done = True
            continue
        rows.append((page.label, page.doc))
    return rows


def render_readme_table(nav: list[NavPage], blurbs: dict[str, str]) -> str:
    """README Documentation table — links are repo-relative (``docs/...``)."""
    lines = ["| Guide | What's in it |", "| --- | --- |"]
    lines += [f"| [{label}](docs/{doc}) | {blurbs[doc]} |" for label, doc in _table_rows(nav)]
    return "\n".join(lines)


def render_index_table(nav: list[NavPage], blurbs: dict[str, str]) -> str:
    """docs/index.md table — links are docs-relative (page lives under ``docs/``)."""
    lines = ["| Guide | What's in it |", "| --- | --- |"]
    lines += [f"| [{label}]({doc}) | {blurbs[doc]} |" for label, doc in _table_rows(nav)]
    return "\n".join(lines)


def render_llms_docs(nav: list[NavPage], blurbs: dict[str, str]) -> str:
    """llms.txt ``## Docs`` — every non-tutorial page (incl. index.md), absolute links."""
    lines = []
    for page in nav:
        if page.group == _TUTORIALS_GROUP:
            continue
        lines.append(f"- [{page.label}]({_SITE}{route_for(page.doc)}): {blurbs[page.doc]}")
    return "\n".join(lines)


def render_llms_tutorials(nav: list[NavPage]) -> str:
    """llms.txt ``## Tutorials`` — the numbered tutorial leaves, absolute links, no blurb."""
    lines = []
    for page in nav:
        if page.group == _TUTORIALS_GROUP and _is_tutorial_leaf(page.doc):
            lines.append(f"- [{page.label}]({_SITE}{route_for(page.doc)})")
    return "\n".join(lines)


# --- bijection / coverage checks --------------------------------------------


def _blurb_required_docs(nav: list[NavPage]) -> list[str]:
    """Docs that must carry a blurb: every nav page except tutorial leaves."""
    return [p.doc for p in nav if not _is_tutorial_leaf(p.doc)]


def missing_blurbs(nav: list[NavPage], blurbs: dict[str, str]) -> list[str]:
    """Nav pages (tutorial leaves exempt) that have no ``extra.docs_index`` blurb."""
    return [doc for doc in _blurb_required_docs(nav) if doc not in blurbs]


def orphan_blurbs(nav: list[NavPage], blurbs: dict[str, str]) -> list[str]:
    """Blurb entries with no matching nav page (renamed/removed page left behind)."""
    required = set(_blurb_required_docs(nav))
    return sorted(k for k in blurbs if k not in required)


def docs_missing_from_nav(repo_root: Path, nav: list[NavPage]) -> list[str]:
    """Published ``docs/*.md`` pages absent from the nav (the caught-nothing bug class)."""
    nav_docs = {p.doc for p in nav}
    missing: list[str] = []
    docs_dir = repo_root / "docs"
    for md in sorted(docs_dir.rglob("*.md")):
        rel = md.relative_to(docs_dir).as_posix()
        if rel in _NAV_EXCLUDE_FROM_NAV_CHECK or rel in nav_docs:
            continue
        missing.append(rel)
    return missing


def tutorials_table_drift(repo_root: Path, nav: list[NavPage]) -> str | None:
    """Assert docs/tutorials/README.md's table links equal the nav's tutorial leaves.

    Checked, not generated — that table carries a "you'll learn" column the nav
    doesn't, so regenerating it would reintroduce a dual source of truth.
    """
    table = repo_root / "docs/tutorials/README.md"
    linked = re.findall(r"\]\((\d\d-[\w-]+\.md)\)", table.read_text(encoding="utf-8"))
    nav_leaves = [p.doc.split("/")[-1] for p in nav if p.group == _TUTORIALS_GROUP and _is_tutorial_leaf(p.doc)]
    if linked != nav_leaves:
        return f"docs/tutorials/README.md links {linked} but nav has tutorial leaves {nav_leaves}"
    return None


# --- rendering to disk -------------------------------------------------------


def _replace_between(text: str, start: str, end: str, body: str) -> str:
    """Replace the content between the ``start`` and ``end`` marker lines with ``body``."""
    pattern = re.compile(re.escape(start) + r"\n.*?\n" + re.escape(end), re.DOTALL)
    if not pattern.search(text):
        raise ValueError(f"marker pair {start!r} .. {end!r} not found in the target file")
    return pattern.sub(lambda _m: f"{start}\n{body}\n{end}", text)


def _rendered_files(repo_root: Path) -> dict[Path, str]:
    """The full intended content of each generated file, keyed by path."""
    mkdocs = repo_root / "mkdocs.yml"
    nav = load_nav(mkdocs)
    blurbs = load_blurbs(mkdocs)

    readme = repo_root / "README.md"
    index = repo_root / "docs/index.md"
    llms = repo_root / "docs/llms.txt"

    out: dict[Path, str] = {}
    out[readme] = _replace_between(readme.read_text(encoding="utf-8"), _START, _END, render_readme_table(nav, blurbs))
    out[index] = _replace_between(index.read_text(encoding="utf-8"), _START, _END, render_index_table(nav, blurbs))
    llms_text = llms.read_text(encoding="utf-8")
    llms_text = _replace_between(llms_text, _START_DOCS, _END_DOCS, render_llms_docs(nav, blurbs))
    llms_text = _replace_between(llms_text, _START_TUT, _END_TUT, render_llms_tutorials(nav))
    out[llms] = llms_text
    return out


def write(repo_root: Path) -> list[Path]:
    """Regenerate all three index surfaces in place. Returns the files touched."""
    return write_all(_rendered_files(repo_root))


def check(repo_root: Path) -> dict[str, str]:
    """Unified diff per file whose generated content differs from disk (empty = clean)."""
    return diff_all(_rendered_files(repo_root))


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[2]
    for p in write(root):
        print(f"wrote {p}")
