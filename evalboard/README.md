# evalboard

Minimal localhost dashboard for coder_eval runs. Next.js App Router, reads runs
from a local runs directory in server components — no database, no persistent
backend.

## Setup

```bash
cd coder_eval/evalboard
pnpm install
EVALBOARD_LOCAL_RUNS_DIR=/path/to/runs pnpm dev
# open http://localhost:3030
```

Point `EVALBOARD_LOCAL_RUNS_DIR` at any coder_eval runs directory; the listing
comes from the filesystem. `pnpm dev:local` is a shortcut for coder_eval's own
`runs/` (relative to `evalboard/`). Only directories containing a `run.json`
show up in the index — empty shells and the `latest` symlink are filtered out.

## Layout

- `/` — the 20 most recent runs, one row each, clickable. Includes a
  daily success-rate chart and tag rails for filtering.
- `/trends` — per-task pass rate and avg duration/cost/turns across the
  last 10 runs, with a tag filter and expandable per-task history.
- `/path-to-ga` — GA-readiness report for the tasks tagged `path-to-ga`.
  Its task table deliberately answers under **stricter rules than every other
  surface**, and both differences are load-bearing:
  1. **De-tagged tasks are dropped.** A `run.json` tag is a historical stamp, so
     elsewhere (including `/trends`) a task de-tagged upstream lingers until the
     last run that predates the removal ages out. Here a task is dropped once a
     *newer* run in the window carries it without the tag — proof of removal.
     A task that merely stopped appearing is unknowable, so it is kept and dated.
  2. **Mature carry-forwards are not passes.** Elsewhere a skipped-but-carried-
     forward row counts as a pass; here it is excluded from both the numerator
     and the denominator, so the rate reports only measured runs (`—` when
     nothing executed).
  The headline tile and chart above the table keep the ordinary mature-blind,
  union-over-window semantics — they feed the front page — which is why they read
  higher than the table, and why the page says so in prose.
- `/watchlist` — what needs attention, ranked over the recent-runs window: tasks
  and skills scored on failures, regressions and turn-budget pressure
  (`lib/watchlist.ts`).
- `/scribe` — the Autopilot (aria/Composer) suite, run by `coder_eval_uipath`'s
  `UiPath.Autopilot.Eval.Manual` pipeline. This is the one surface that reads a
  **different blob container** (`aria-runs`, not `runs`) — see *Sources* below.
- `/runs/latest` — shortcut that redirects to the newest run id.
- `/runs/<run-id>` — run summary (pass rate, cost, duration) + one row per task.
  A "Download run (.zip)" button bundles the whole run folder.
- `/runs/<run-id>/<task-id>` — per-task detail: success-criteria cards,
  artifact downloads, flow debug table, tool timeline, message timeline
  (per-message generation / exec time and output / cache-write / cache-read
  tokens, with each row expandable into thinking / tool / text sub-rows),
  tail of `task.log`. A "Download folder (.zip)" button bundles this task's
  folder.

`<task-id>` is the same string the eval framework writes to
`task_results[].task_id` (e.g., `skill-flow-calculator`) and equals the
subdir name under `<run-id>/<variant-id>/`.

## Variants (A/B runs)

A coder_eval experiment that declares `variants:` runs every task once per arm
and writes each arm to its own subtree (`<run-id>/<variant-id>/<task-id>/<NN>/`),
stamping `variant_id` on every `run.json` row. An experiment with no `variants:`
still writes one arm, named `default` — which is why a single-arm run and a
multi-arm run have the same shape on disk.

Evalboard reads that directly:

- The run page's **Pass rate tile reports one entry per arm** (side by side, so
  the tile keeps the height of the single-arm version and the tiles beside it are
  not stretched), and reports no pooled rate at all. A blended rate would average configurations that were
  deliberately made to differ (and would move when the arms are merely
  reordered), so on a variant run it is not a number anyone wants. The tile
  states the observed `spread` between arms and nothing more.
- **Total cost and Time keep their pooled totals** and carry the per-arm split on
  their sub-line, in place of p50/p90. A run's spend is a real operational
  number however many arms produced it, in a way a run's pass rate is not.
- The task grid gains a **Variant** column and keeps one row per (task, arm).
  Replicates of one arm still collapse into a single row; **arms never collapse
  into each other** — that difference is the measurement. The default ordering
  ranks a *task* by its worst arm rather than ranking each row on its own, so
  failures still sort to the top while a task's arms stay adjacent. Ranking rows
  independently splits exactly the pairs worth reading: a task whose arms
  disagree ends up with one row at the top of the grid and the other at the
  bottom.
- Task detail is addressed by `?v=<variant-id>` alongside `?r=<replicate>`, so
  each arm opens its own transcript, log, criteria and artifacts. A link that
  names no arm resolves to the task's first — `default` on an ordinary run, the
  leading arm on an experiment run, whose arms are all named and which therefore
  has no `default` subtree for a bare link to land in. A link naming an arm the
  run does not have is a 404, not a fallback: the point of addressing an arm is
  that you get that arm or nothing.

Every one of those is inert on a run without variants: the column is dropped, the
tiles keep their existing pooled numbers and percentiles, and no link carries
`?v=`. Cross-run comparison (two separate run ids) is a different feature and is
not what this does.

Statistics are deliberately not computed here. Whether a gap between arms is
real is a question about variance, and coder_eval already answers it in the
experiment report it writes beside `run.json` (`experiment.md` — win rates,
per-task comparison, most divergent tasks, paired comparison with Welch t-test
and bootstrap CIs).

## Sources

A **source** is one blob container of runs, surfaced as its own tab
(`lib/sources.ts`). One deployment serves all of them — the container is a
runtime dimension threaded through the data layer as a trailing
`source: Source = DEFAULT_SOURCE` parameter, not a build-time env var:

| Source | Container | Surface |
|--------|-----------|---------|
| `skills` (default) | `runs` | Everything not listed below |
| `scribe` | `aria-runs` | `/scribe` |

Non-default sources are selected by a `?src=<id>` query param, which every
run-scoped page and API route reads. An absent or unrecognised `src` resolves to
the default source (`sourceById` coerces rather than throwing, so a stray param
in a shared link degrades to the skills dashboard instead of an error page).

Two invariants worth preserving if you add a source:

- **Run ids are only unique within a container.** Every suite names runs
  `YYYY-MM-DD_HH-MM-SS`, so the same id routinely exists in two containers. This
  is why each source gets its own cache dir (`runsDirFor`), why `lib/blob.ts`
  scopes its in-flight dedupe keys by container, and why `unstable_cache` keys
  in `lib/overview.ts` and `lib/trends.ts` all carry `source.id`. Drop any one of
  those and one source starts serving another's data for a colliding id — with no
  error.
- **A run whose id is not date-shaped is invisible to the windowed views.**
  `getRunListing`, `loadRecentRunsInner`, `getOverview` and
  `listRunIdsInWindow` filter on `parseRunIdDate`, so such runs surface only in
  the ad-hoc section. A new source's page therefore needs its OWN
  `getAdhocRunListing` section, or ad-hoc uploads to that container land
  nowhere reachable.
- **Local mode is per-source too.** `listRunIds` resolves
  `runsDirFor(RUNS_DIR, source)` when `EVALBOARD_LOCAL_RUNS_DIR` is set, so
  `/scribe` reads `<local>-scribe`. Listing off the bare local dir instead —
  which is what shipped first — returns the *default* source's ids for every
  source while the readers resolve under the sibling, so the listing and the
  reads disagree about which container they describe.
  `lib/__tests__/source-isolation.test.ts` pins both halves; it's the only test
  that exercises the reader layer, where the invariant above actually lives.

## Conventions

- `/api/file?run=<id>&path=<relpath>[&src=<source>]` serves `.flow`, `.uipx`,
  etc. with path-traversal guard (`resolveSafePath`).
- `/api/download?run=<id>[&task=<id>][&v=<variant>][&src=<source>]` streams a zip
  of a task folder (with `task`, from the named arm) or the whole run (without).
  Files are gathered by `collectTaskFiles` / `collectRunFiles`, which reuse the
  `walkArtifacts` noise filter, and zipped by `lib/zip.ts` (a dependency-free
  DEFLATE writer).
- Pass rows render green (`bg-green-50 text-green-700`), failures render red
  (`bg-red-50 text-red-700`), on a white background.
