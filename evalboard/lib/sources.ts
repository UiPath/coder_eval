// This module is imported by CLIENT components (via app/_lib/source-param.ts,
// which builds hrefs in task-grid / activation-card / refresh-button), so it must
// stay free of Node builtins — importing `node:path` here fails the webpack
// client build with an UnhandledSchemeError. Keep it pure data + string helpers.
//
// A "source" is one blob container of runs, surfaced as its own top-level tab.
// One evalboard deployment serves every source — the container is a runtime
// dimension threaded through the data layer, NOT a build-time env var, so the
// same site can show the skills nightly and the Autopilot/aria suite side by
// side.
//
// Runs from different sources must never share a cache directory: run ids are
// only unique WITHIN a container (both suites name runs `YYYY-MM-DD_HH-MM-SS`,
// so a same-day skills run and aria run collide on id). Each source therefore
// gets its own cache subtree — see `runsDirFor`.
export interface Source {
    /** URL/query token. Must satisfy blob.ts's ID_RE — it lands in filesystem paths. */
    id: string;
    /** Display name, used for the nav tab and page headings. */
    label: string;
    /** Azure blob container holding this source's runs. */
    container: string;
}

// The canonical skills nightly. Keeps the bare `runs` container and the
// un-namespaced cache dir so existing deployments' on-disk caches, and
// EVALBOARD_LOCAL_RUNS_DIR pointing at a real coder_eval `runs/` tree, keep
// working untouched.
export const SKILLS_SOURCE: Source = {
    id: "skills",
    label: "Skills",
    container: "runs",
};

// The Autopilot (aria/Composer) suite, uploaded by coder_eval_uipath's
// `eval-runner-autopilot` — see that repo's .azure-pipelines/autopilot-eval-daily.yml.
// Named "Scribe" after the pipeline's Key Vault (coder-eval-proc-scribe).
export const SCRIBE_SOURCE: Source = {
    id: "scribe",
    label: "Scribe",
    container: "aria-runs",
};

export const SOURCES: readonly Source[] = [SKILLS_SOURCE, SCRIBE_SOURCE];

/** Every surface that doesn't opt into a source reads the skills nightly. */
export const DEFAULT_SOURCE = SKILLS_SOURCE;

export function sourceById(id: string | null | undefined): Source {
    if (!id) return DEFAULT_SOURCE;
    return SOURCES.find((s) => s.id === id) ?? DEFAULT_SOURCE;
}

/**
 * Local cache root for a source, derived from the shared `RUNS_DIR` base.
 *
 * The default source keeps `base` unchanged, so existing on-disk caches and a
 * local-mode `EVALBOARD_LOCAL_RUNS_DIR` pointing at a real coder_eval `runs/`
 * tree keep resolving exactly as before. Other sources get a SIBLING directory
 * (`<base>-<id>`) rather than a subdirectory of `base`: a nested cache dir would
 * show up as an entry when listing the default source's runs, and would only be
 * excluded by the incidental fact that it contains no run.json. A sibling can't
 * be mistaken for a run at all.
 */
export function runsDirFor(base: string, source: Source): string {
    if (source.id === DEFAULT_SOURCE.id) return base;
    // Suffix the final segment. Done with string ops rather than path.dirname +
    // path.join to keep this module client-safe (see the note at the top);
    // `base` is already an absolute resolved path in every real caller, since
    // RUNS_DIR is resolved at module load in lib/runs.ts.
    const trimmed = base.replace(/[/\\]+$/, "");
    return `${trimmed}-${source.id}`;
}
