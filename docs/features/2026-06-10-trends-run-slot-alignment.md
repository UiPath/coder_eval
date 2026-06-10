# Trends: run-slot-aligned status strips + ad-hoc-proof recent window

**Status:** implemented · **Date:** 2026-06-10

## Why

A report that "a lot of Flow tasks do not have recent trends data (some tests
seem to have gaps)" on `/trends?tag=uipath-maestro-flow` turned out to be
ordinary task churn in the skills repo, rendered illegibly:

- skills #1245 deleted `skill-flow-ixp-activation-listing`; skills #1290
  renamed `ixp-activation*` → `ixp-routing*` and `ixp-smoke-scaffold-*` →
  `ixp-scaffold-*` while adding the `routing_listing` dataset. Trends are
  keyed by `task_id`, so the renamed tests' history stops mid-window and the
  new ids restart from zero. Cross-checking the task set of every recent
  `run.json` against the skills repo confirmed **zero data loss** — every
  currently-defined flow task is present in the newest runs.
- The trends "Recent" strip rendered only the runs a task appeared in, as a
  contiguous left-to-right sequence. A renamed-away test's last bar sat flush
  with the right edge — visually indistinguishable from a test that ran this
  morning — and a brand-new test looked identical to a long-running one.

## What changed

1. **Run-slot alignment.** `aggregate()` now returns `TrendsData { runIds,
   trends }` where `runIds` is the newest-first run axis in scope (runs whose
   `run.json` failed to load or contained no tasks are excluded). `StatusBar`
   slots each task's statuses onto that shared axis and renders an explicit
   gray "not in run" placeholder for runs without an entry, so renames,
   retirements, and late additions are visible instead of silently
   compressed. The page provenance line is derived from the same cached
   aggregate, so the label and the strips cannot skew apart. The
   `unstable_cache` key is bumped (`aggregate-task-trends-v2`) because the
   cached shape changed.

2. **Unusable runs no longer shrink the window.** `loadRecentRunsInner`
   sliced the newest `limit` date-shaped ids **before** dropping runs flagged
   `adhoc` in `meta.json` — each date-named ad-hoc upload silently cost the
   trends page one run of history. The same slot-wasting applied to runs
   whose `run.json` couldn't be read (transient blob failures are downgraded
   to `overview: null` and cached for 5 minutes; an aborted upload without a
   `run.json` is permanent) and to zero-task runs — all of which the axis
   excludes anyway. The new `collectPipelineRuns` helper backfills past every
   unusable run: the first round fetches exactly `limit` ids (common case =
   one fetch round), refill rounds carry a minimum batch size so a stubborn
   deficit doesn't degenerate into one-id-per-round serial loads, and a total
   scan cap (3× the window) keeps a pathological stretch of unusable runs —
   e.g. a blob outage failing every load — from walking the entire container
   (a warning is logged when the cap truncates the window). The watchlist
   page consumes the same `loadRecentRuns` and gains the identical backfill —
   its window no longer shrinks on ad-hoc/broken runs either (a semantic
   no-op otherwise: `buildWatchlist` already skipped null-overview runs).

## Verification

`pnpm verify` (tsc + vitest + next build), plus a local-mode render against
the real last-12 `run.json` blobs: the renamed `ixp-activation/explicit` row
shows statuses 05-28 → 06-05 followed by three "not in run" slots, and the
provenance reads "Last 10 runs · 2026-05-27 → 2026-06-10" with the date-named
ad-hoc run (`2026-06-10_10-01-09`) correctly excluded without costing a slot.
