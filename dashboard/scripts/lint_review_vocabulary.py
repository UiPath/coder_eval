"""Report tags appearing in review.json files that aren't in the suggested vocabulary.

The /coder-eval-review skill emits an open vocabulary — it prefers names from
src/coder_eval/resources/tags.yaml but isn't blocked from coining new ones.
This script surfaces the new ones so a human can promote good ones to the
vocabulary and flag near-duplicates / typos.

Usage:
  python -m dashboard.scripts.lint_review_vocabulary <runs_root>

<runs_root> is a local directory containing pulled run dirs (e.g. ``runs/``
in the coder_eval checkout, or a ``tmp/runs/`` cache after
``dashboard/scripts/pull-run.sh``). To lint runs in blob, pull them locally
first.

Exits 0 even when drift is present — the script reports, it doesn't gate.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import click
import yaml

from dashboard.config import CODER_EVAL_DIR

VOCABULARY_PATH = CODER_EVAL_DIR / "src" / "coder_eval" / "resources" / "tags.yaml"


def load_vocabulary(path: Path = VOCABULARY_PATH) -> set[str]:
    if not path.exists():
        return set()
    data = yaml.safe_load(path.read_text()) or {}
    return {t["name"] for t in (data.get("tags") or []) if isinstance(t, dict) and "name" in t}


def collect_tags(runs_root: Path) -> Counter:
    """Walk runs_root for review.json files and count tag occurrences."""
    counts: Counter = Counter()
    for review in runs_root.glob("*/*/*/*/review.json"):
        try:
            payload = json.loads(review.read_text())
        except json.JSONDecodeError:
            continue
        for tag in payload.get("tags", []) or []:
            if isinstance(tag, str):
                counts[tag] += 1
    return counts


@click.command()
@click.argument("runs_root", type=click.Path(exists=True, file_okay=False, path_type=Path))
def main(runs_root: Path) -> None:
    vocab = load_vocabulary()
    counts = collect_tags(runs_root)

    if not counts:
        click.echo(f"No review.json files found under {runs_root}.")
        return

    in_vocab = {t: n for t, n in counts.items() if t in vocab}
    drift = {t: n for t, n in counts.items() if t not in vocab}

    click.echo(f"Vocabulary: {len(vocab)} canonical tags. Reviews scanned under {runs_root}.\n")
    click.echo("In-vocabulary tags:")
    for tag, n in sorted(in_vocab.items(), key=lambda x: (-x[1], x[0])):
        click.echo(f"  {tag:32s}  x {n}")

    if drift:
        click.echo("\nDrift (tags emitted by classifier that aren't in tags.yaml):")
        for tag, n in sorted(drift.items(), key=lambda x: (-x[1], x[0])):
            click.echo(f"  {tag:32s}  x {n}")
        click.echo(f"\nTo promote: add an entry to {VOCABULARY_PATH.relative_to(CODER_EVAL_DIR)} and open a PR.")
    else:
        click.echo("\nNo drift detected.")


if __name__ == "__main__":
    main()
