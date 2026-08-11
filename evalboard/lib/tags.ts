// The repo-provenance tag predicate, and nothing else.
//
// Deliberately dependency-free (no next/*, no blob readers) so a "use client"
// component can import it. That is the whole reason it is not in lib/overview.ts:
// that module imports next/cache and the blob readers, so anything defined there
// is server-only, which is why app/runs/[id]/run-view.tsx and lib/trends.ts each
// grew their own inline copy of these two lines.
//
// The parameter is structurally typed rather than RunOverviewTask so all three
// shapes that carry repo provenance — RunOverviewTask, TaskResultSummary and the
// aggregated TaskTrend — satisfy it without importing each other.

export interface RepoTagged {
    skill: string | null;
    tags: string[];
}

// The tag as the task's own YAML declared it, stamped into run.json at execution
// time. This is the repo-provenance HALF of taskMatchesTag: the only half whose
// absence in a newer run proves the tag was removed upstream, since review tags
// are post-hoc annotations and an unreviewed run carries none.
export function taskCarriesRepoTag(task: RepoTagged, tag: string): boolean {
    return task.skill === tag || task.tags.includes(tag);
}
