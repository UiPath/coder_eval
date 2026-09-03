import { promises as fs } from "node:fs";
import path from "node:path";
import {
    LOCAL_RUNS_DIR,
    ensureActivationSummary,
    ensureRunAnalysis,
    ensureRunDir,
    ensureRunMeta,
    ensureRunSummary,
    ensureTaskDir,
    isValidId,
    isValidTaskId,
    listRunIdsLocal,
    listRunIdsRemote,
} from "./blob";
import { DEFAULT_VARIANT_ID, isValidVariantId } from "./variants";
import { DEFAULT_SOURCE, runsDirFor, type Source } from "./sources";
import { DELIVERABLE_KINDS, DELIVERABLE_NAMES } from "./artifact-kinds";
import { messageCostUsd } from "./pricing";

// Resolution order:
//   1. EVALBOARD_LOCAL_RUNS_DIR — local mode, points at a coder_eval runs dir
//      (no blob, no caching).
//   2. EVALBOARD_RUNS_DIR — blob-mode cache override, used when process.cwd()
//      is read-only (e.g., App Service Run From Package).
//   3. ./runs-remote — default blob-mode cache.
//
// This is the BASE cache dir. Every run-scoped read resolves it per source via
// runsDirFor(RUNS_DIR, source), since run ids collide across containers.
export const RUNS_DIR = LOCAL_RUNS_DIR
    ? path.resolve(LOCAL_RUNS_DIR)
    : (process.env.EVALBOARD_RUNS_DIR ??
      path.resolve(process.cwd(), "runs-remote"));

// ---------- Types ----------

export interface ComponentSha {
    name: string; // "coder_eval" | "skills" | "cli"
    sha: string;
    // null when value isn't a real commit (e.g. "unknown", or a package tag
    // like "alpha" since #276 switched to consuming GitHub Packages instead
    // of building from source).
    url: string | null;
}

// Git SHAs are hex, 7-40 chars. Non-matching strings are version labels
// ("alpha", "stable", "v1.2.3") and shouldn't be linked to github.com/.../tree/<x>.
const SHA_RE = /^[0-9a-f]{7,40}$/i;

// A run is one `coder-eval run` invocation. It contains N task results.
// Run-level summary stats. Fields aggregate across all tasks in the run.
export interface RunSummary {
    id: string;
    startTime: string | null;
    endTime: string | null;
    // Sum of per-task durations (compute time). Falls back to wall-clock
    // (run end - run start) when per-task durations are unavailable.
    taskDurationSeconds: number | null;
    tasksRun: number;
    tasksSucceeded: number;
    tasksFailed: number;
    tasksError: number;
    tasksNotGraded: number;
    totalCostUsd: number | null;
    componentShas: ComponentSha[];
    // What actually produced this run, for the run header. `harness` is the
    // coder-eval AgentKind from the RunConfig stamp (falling back to the per-task
    // agent_config vote); null only on legacy runs that identify neither.
    harness: string | null;
    // Dominant per-task `model_used`, with how many distinct models the run
    // touched. Normally 1 — a run is one model — but an A/B experiment fans
    // variants across models in a single run, and a header claiming one model
    // would be wrong there, so the count is carried and surfaced.
    model: string | null;
    modelCount: number;
}

export interface TaskResultSummary {
    taskId: string;
    // Experiment arm this row belongs to. A run with variants writes one row per
    // (task, variant, replicate) and stores each arm's content under its own
    // <runId>/<variantId>/ subtree, so this — not taskId alone — is what tells
    // two arms of the same task apart. Null on runs whose run.json predates the
    // field; every reader falls back to DEFAULT_VARIANT_ID, which is the segment
    // a single-arm run writes anyway.
    variantId: string | null;
    // Replicate index of this row, or null when repeats are disabled / on legacy
    // runs. Repeated runs share taskId, so this disambiguates sibling rows and is
    // carried in the per-task link (?r=NN) so each replicate opens its own detail.
    replicateIndex: number | null;
    status: string | null;
    weightedScore: number | null;
    durationSeconds: number | null;
    totalCostUsd: number | null;
    actualCommands: number | null;
    totalTurns: number | null;
    expectedTurns: number | null;
    // Wall clock this task is expected to need, derived per harness from run
    // history and stamped into run.json by the eval runner. Null = unscored
    // (too little history, or a run predating the stamp) — never "on target".
    // Runs alongside expectedTurns while the turn budget is still reported.
    expectedSeconds: number | null;
    // True when the agent's final iteration emitted a text reply
    // (i.e. ResultMessage.result was non-empty). Lets grid/trends
    // Turns cells inflate by +1 on legacy runs that lack total_turns.
    hasFinalReply: boolean;
    // Per-task token totals from run.json. Null on legacy runs that
    // don't record per-task token counts. `inputTokens` is the disjoint
    // uncached slice (run.json `input_tokens` is serialized from
    // TokenUsage.uncached_input_tokens — see reports_experiment.py), so it
    // sits alongside the cache columns without overlap.
    inputTokens: number | null;
    outputTokens: number | null;
    cacheCreationTokens: number | null;
    cacheReadTokens: number | null;
    // Model the task ran on (run.json `model_used`). Used to price token
    // buckets as USD for the Tokens↔USD column toggle. Null on legacy runs.
    model: string | null;
    tags: string[];
    // Derived primary group. See deriveSkill below for the resolution chain
    // (new runs use task_path; older runs fall back to a tag heuristic).
    skill: string | null;
    // True when this row is a "mature" task the nightly skipped this run and
    // carried forward as a pass (run.json `mature_skipped`). The task wasn't
    // executed, so it has no per-task detail to link to. False on normal rows.
    matureSkipped: boolean;
}

export interface CriterionResult {
    criterionType: string | null;
    description: string | null;
    score: number | null;
    details: string | null;
    error: string | null;
    evaluationStatus: "evaluated" | "not_evaluated";
    // Mirrors the Python CriterionResult fields. `gating: false` (weight: 0) means
    // the criterion is informational — measured, but excluded from the score and
    // the pass/fail gate, so it must not render as PASS/FAIL. Both default the way
    // pre-existing task.json files behave: gating, threshold 0.9.
    passThreshold: number;
    gating: boolean;
}

interface RawCriterionResult {
    criterion_type?: string;
    description?: string;
    score?: number;
    details?: string;
    error?: string | null;
    evaluation_status?: "evaluated" | "not_evaluated";
    pass_threshold?: number;
    gating?: boolean;
}

export function parseCriterionResults(
    criteria: RawCriterionResult[] | undefined,
): CriterionResult[] {
    return (criteria ?? []).map((criterion) => ({
        criterionType: criterion.criterion_type ?? null,
        description: criterion.description ?? null,
        score: criterion.score ?? null,
        details: criterion.details ?? null,
        error: criterion.error ?? null,
        evaluationStatus: criterion.evaluation_status ?? "evaluated",
        passThreshold: criterion.pass_threshold ?? 0.9,
        gating: criterion.gating ?? true,
    }));
}

export interface ElementExecution {
    elementId: string;
    elementType: string | null;
    status: string;
    startedAt: string | null;
    completedAt: string | null;
    errorMessage: string | null;
    outputPreview: string | null;
}

export interface FlowDebugResult {
    finalStatus: string | null;
    studioWebUrl: string | null;
    jobKey: string | null;
    elements: ElementExecution[];
}

export interface ToolCall {
    index: number;
    tool: string;
    summary: string;
}

// One assistant message in the SDK transcript. Captured per `iterations[i].
// messages[j]` and used by the message-timeline view. Includes:
//   - timing: generation_duration_ms, and any captured tool executions that
//     this message kicked off (matched by tool_use_id).
//   - content: a flattened list of typed blocks (thinking / tool_use / text).
export interface MessageEvent {
    index: number;                // 1-based order across the whole task
    // "assistant" = a real LLM generation. "reconciliation" = the synthetic
    // terminal entry (one per turn) carrying tokens the agent billed but never
    // surfaced as a generation — so summing the four token buckets across the
    // stream reproduces the authoritative turn total. user/system live in
    // iteration metadata, not here.
    role: "assistant" | "reconciliation";
    startedAt: string | null;
    completedAt: string | null;
    generationMs: number | null;  // LLM generation time for this message
    thinkingMs: number | null;    // portion of generationMs attributable to thinking blocks
    textMs: number | null;        // portion of generationMs attributable to text blocks
    toolGenMs: number | null;     // portion of generationMs attributable to tool_use blocks
    blockTypes: ("thinking" | "tool_use" | "text")[];
    thinkingText: string | null;  // concatenated thinking blocks
    text: string | null;          // concatenated text blocks (final reply chunks)
    toolUses: MessageToolUse[];   // resolved against iteration.commands by tool_use_id
    // Per-message token usage from the Anthropic API. Summed across grouped
    // raws (one logical emission may span multiple MessageEntry rows when
    // the CLI splits content blocks). null when no raw in the group carried
    // a usage figure — older runs predating per-message tokens.
    inputTokens: number | null;
    outputTokens: number | null;
    cacheWriteTokens: number | null;
    cacheReadTokens: number | null;
    // Branch identity for the prompt-cache cascade tree: the Task tool_use_id
    // that spawned this message's sub-agent, null for the main thread, or
    // `undefined` when the run never recorded it (legacy). Calls grouped by this
    // value form a branch whose context is re-read only within itself — a
    // sub-agent's tokens don't cascade into the main thread or siblings.
    parentToolUseId: string | null | undefined;
    // Anthropic's `reasoning_tokens`. ~Always 0 from the SDK, so it is NOT
    // used to attribute thinking output — see thinkingOutputTokens. Kept for
    // completeness. null when no raw in the group recorded it.
    reasoningTokens: number | null;
    // Output tokens attributed to the thinking block(s), taken from the real
    // per-emission output_tokens of the thinking emission (the agent splits a
    // call's output across its blocks by content length). null when no output
    // was recorded or the group has no thinking block.
    thinkingOutputTokens: number | null;
    // Output tokens attributed to the text block(s) — the text emission's own
    // recorded output_tokens. Null when there's no output figure or no text block.
    textOutputTokens: number | null;
    // Model id on the emission (e.g. "claude-sonnet-4-6"). Consumed by the
    // cascade-aware thinking-cost simulator to price each message. null when
    // no raw in the group recorded it.
    model: string | null;
    // Rate-card ESTIMATE of this message's cost in USD, priced from the
    // per-message token buckets and model against the shared rate table
    // (lib/pricing.ts). The SDK only reports a cumulative per-turn cost, so this
    // is the rate-accurate per-message attribution. Actual per-call cost (LiteLLM
    // backend) is NOT distributed here — it lives in the separate providerCalls
    // per-call table. null when the model is unpriced or no token figure was
    // recorded (older runs).
    costUsd: number | null;
    // Only set on a `reconciliation` row: the human-readable explanation of why
    // these tokens are unattributed (e.g. fixed prompt overhead + sub-agent
    // input the stream doesn't bubble up). null on assistant rows.
    note: string | null;
}

export interface MessageToolUse {
    toolName: string;
    toolUseId: string | null;
    // Human-friendly summary (description-first). Kept for compatibility with
    // anything that already consumes the old single-string display.
    summary: string;
    // The actual operative argument the agent passed: shell command for Bash,
    // file_path for Read/Write/Edit, skill id for Skill, pattern for Grep, etc.
    // This is what you'd run to reproduce the call. Null only when no
    // recognizable key was present.
    argText: string | null;
    // The optional `description` field on the tool call — agent's stated intent.
    // Only set when distinct from argText and present in the params.
    description: string | null;
    genMs: number | null;         // LLM generation time for this tool_use block
    durationMs: number | null;    // tool execution time (separate from generationMs)
    isError: boolean;
    resultPreview: string | null; // short truncated preview of the result
    // Output tokens for this tool_use — the tool emission's recorded
    // output_tokens (the agent records output per block-emission). Only when a
    // single emission carries multiple parallel tool_uses is it split among
    // them (by arg-size proxy). null when no output_tokens was recorded.
    outputTokens: number | null;
    // Approx token size of THIS tool's result content (measured from the
    // untruncated result, cache-independent). Drives the cost simulator's
    // tool-output lever — works whether or not prompt caching was enabled. null
    // on runs predating the field.
    resultTokens: number | null;
}

export interface ArtifactRef {
    relPath: string;
    // Lowercased file extension without the dot ("flow", "md", "json"), or
    // "file" when the name has no extension. Drives the KindChip label/color.
    kind: string;
    sizeBytes: number;
}

export interface TaskDetail extends TaskResultSummary {
    runId: string;
    // Component/tool versions parsed from THIS task's own task.json
    // environment_info (cli + @uipath/*-tool plugins) — the versions that
    // actually ran for this task, which can diverge from the run-level
    // aggregate when tool resolution drifts mid-run (a task that invokes
    // `uip <tool>` can pull a different alpha than its siblings). Empty for
    // legacy task.json files captured before environment_info existed.
    componentShas: ComponentSha[];
    finalStatus: string | null;
    errorMessage: string | null;
    taskDescription: string | null;
    criteria: CriterionResult[];
    postFailureCriteria: CriterionResult[];
    artifacts: ArtifactRef[];
    flowDebug: FlowDebugResult | null;
    toolCalls: ToolCall[];
    // Final text-only assistant message (no tool call) — sourced from the
    // last turn's ResultMessage.result. Renders as the trailing entry in
    // the Turn timeline so the (5) header reconciles with the 4 tool calls.
    finalAssistantText: string | null;
    // Per-assistant-message timeline data. Empty array for legacy task.json
    // files that don't have iterations[].messages; the message-timeline
    // section then doesn't render.
    messages: MessageEvent[];
    // Per-task token totals summed across iterations. All zeros for legacy
    // runs that don't record per-iteration token_usage.
    tokens: TokenTotals;
    // Per-sub-agent token breakdown, derived by grouping the parsed messages on
    // `parentToolUseId` (the spawning Agent call's tool_use_id). Each sub-agent's
    // generations are tagged with that id — Codex's child generations are
    // reconstructed from its rollout, and Claude's terminal generation is
    // synthesized as one too — so the breakdown is complete and keyed by the
    // spawning tool_use_id (the message timeline can therefore join it back to
    // the Agent row). The cost simulator consumes the values via Object.values().
    // Empty for runs/turns with no spawned sub-agents.
    subAgentUsageByToolId: Record<string, SubAgentTotals>;
    // Per-call ACTUAL cost + cache audit rows, grouped by turn iteration. Only
    // turns whose `provider_call_costs` list is non-empty appear (LiteLLM/
    // open-weight backend; empty on Claude/Bedrock). Rendered as a standalone
    // per-call table, NOT distributed onto individual transcript messages.
    providerCalls: { iteration: number; calls: ProviderCallEntry[] }[];
}

// One per-call audit row from `TurnRecord.provider_call_costs` — the actual
// cost + cache split the LiteLLM proxy captured for a single upstream call
// (open-weight backend). The turn-level total_cost_usd remains the real bill;
// these rows are an itemized breakdown surfaced as a separate table. Empty list
// on non-LiteLLM backends.
export interface ProviderCallEntry {
    callId: string | null;
    costUsd: number | null;
    inputTokens: number | null;
    cacheReadTokens: number | null;
    cacheWriteTokens: number | null;
    outputTokens: number | null;
}

// Full per-sub-agent token breakdown (all components, cache-read included).
export interface SubAgentTotals {
    total: number;
    input: number;
    output: number;
    cacheCreation: number;
    cacheRead: number;
}

// Group the parsed assistant messages by `parentToolUseId` into a per-sub-agent
// token breakdown. A sub-agent's generations all carry the spawning Agent call's
// tool_use_id; main-thread messages (parentToolUseId null/undefined) are skipped.
// Keyed by that tool_use_id so the timeline can join it to the Agent row, with
// the values flowing to the cost simulator via Object.values(). Empty when no
// message is parented to a sub-agent spawn.
export function aggregateSubAgentUsage(
    messages: MessageEvent[],
): Record<string, SubAgentTotals> {
    const out: Record<string, SubAgentTotals> = {};
    for (const m of messages) {
        if (m.role === "reconciliation") continue; // not a sub-agent generation
        const toolId = m.parentToolUseId;
        if (!toolId) continue;
        const input = m.inputTokens ?? 0;
        const output = m.outputTokens ?? 0;
        const cacheCreation = m.cacheWriteTokens ?? 0;
        const cacheRead = m.cacheReadTokens ?? 0;
        const prev = out[toolId];
        out[toolId] = {
            input: (prev?.input ?? 0) + input,
            output: (prev?.output ?? 0) + output,
            cacheCreation: (prev?.cacheCreation ?? 0) + cacheCreation,
            cacheRead: (prev?.cacheRead ?? 0) + cacheRead,
            total: (prev?.total ?? 0) + input + output + cacheCreation + cacheRead,
        };
    }
    return out;
}

// ---------- run.json schema ----------

interface RawTaskResult {
    task_id?: string;
    // Experiment arm that produced this row (the <variant> sub-dir). Written by
    // reports_experiment.py on every run; absent on runs that predate it, which
    // read as DEFAULT_VARIANT_ID.
    variant_id?: string | null;
    // Replicate index of this row (the <variant>/<task>/<NN> sub-dir). Repeated
    // runs of one task share a task_id; this is what tells the rows apart. Null
    // on runs that didn't track replicates (repeats disabled / legacy run.json).
    replicate_index?: number | null;
    status?: string;
    weighted_score?: number;
    duration?: number;
    total_cost_usd?: number;
    input_tokens?: number | null;
    output_tokens?: number | null;
    cache_creation_input_tokens?: number | null;
    cache_read_input_tokens?: number | null;
    actual_commands?: number;
    // Cumulative SDK turn count + configured target. Absent on runs from
    // before the dashboard-expected-turns PR; both fields are optional and
    // null-fallback through the cell helpers in lib/turns.ts.
    total_turns?: number;
    expected_turns?: number | null;
    // Derived expected wall clock for this task, stamped by the eval runner
    // (see eval_runner/skills/timing.py). Absent on unscored tasks and on every
    // run predating the stamp, which read as unscored through lib/timing.ts.
    expected_seconds?: number | null;
    // Documented visible-turn count (tool calls + final reply) — the canonical
    // metric the "within expected turns" chart compares against expected_turns.
    // Absent on runs predating this field; visibleTurnsFromRaw() then reconstructs
    // it from actual_commands + has_final_reply (the identical formula) so the
    // metric still populates for historical runs.
    visible_turns?: number | null;
    // True iff the final iteration's ResultMessage.result was non-empty.
    // Absent on legacy runs predating the field — treated as false.
    has_final_reply?: boolean;
    tags?: string[];
    // Source YAML path. Persisted by coder-eval starting with the
    // task_path PR; absent on older runs (deriveSkill falls back to tags).
    task_path?: string | null;
    // Model the task ran on (e.g. "claude-sonnet-4-6"). Used to price token
    // buckets as USD. Absent on legacy runs.
    model_used?: string | null;
    // Per-task agent config; `type` is the harness (coder-eval AgentKind, e.g.
    // "claude-code" | "codex" | "antigravity"). Used to derive the run's harness
    // when the run-level RunConfig stamp is absent (direct coder-eval / legacy runs).
    agent_config?: { type?: string | null } | null;
    // Activation enrichment the dashboard folds onto each case row in the nested
    // activation run.json (see cli.py _finalize_activation_run). Absent on skills
    // rows. `prompt` = the case's prompt text; `expected_skill` = the skill the
    // prompt targets ("" for a negative case); `triggered_skill` = the skill that
    // actually fired ("" = none fired). A mismatch between the two is a mistake.
    prompt?: string | null;
    expected_skill?: string | null;
    triggered_skill?: string | null;
    // Set by the nightly runner (eval_runner) on a carried-forward row for a
    // "mature" task it skipped this run. Absent on normal rows.
    mature_skipped?: boolean;
}

interface RawRunJson {
    run_id?: string;
    start_time?: string;
    end_time?: string;
    total_duration_seconds?: number;
    tasks_run?: number;
    tasks_succeeded?: number;
    tasks_failed?: number;
    tasks_error?: number;
    // Rows that ran but were never scored (`coder-eval execute`). Optional: a
    // run.json written before the field existed simply has none.
    tasks_not_graded?: number;
    task_results?: RawTaskResult[];
    // Values are scalars except `tool_plugins`, a {plugin: version} map of
    // the installed @uipath/*-tool packages (recorded since coder_eval #366).
    environment_info?: Record<
        string,
        string | number | null | Record<string, string>
    >;
    // Per-skill activation rollup. Present ONLY on the nested activation sub-run's
    // run.json (run_id/activation/run.json), which the dashboard finalizes with
    // compute_activation_rollup. The top-level skills run.json never carries it.
    activation?: RawActivation;
    // Run-level wall-clock rollup stamped by the eval runner alongside the
    // per-task expected_seconds. Absent on runs predating it.
    timing?: RawTiming;
}

// The `timing` block from run.json. `tolerance` is recorded by the runner so the
// dashboard can never disagree with the run it is describing about what counted
// as "within expected".
interface RawTiming {
    harness?: string;
    pool_runs?: number;
    time_per_passed_task?: number | null;
    scored_tasks?: number;
    unscored_tasks?: number;
    within_expected_time?: number;
    within_expected_rate?: number | null;
    tolerance?: number;
}

interface RawActivation {
    score?: number;
    denominator?: number;
    min_prompts?: number;
    n_skills_sampled?: number;
    n_cases?: number | null;
    per_skill?: {
        skill?: string;
        recall_yes?: number | null;
        n_prompts?: number;
        sampled?: boolean;
        contributes?: number;
    }[];
}

export interface ActivationSkillScore {
    skill: string;
    recallYes: number | null;
    nPrompts: number;
    sampled: boolean; // ran a full sample (>= minPrompts positive prompts)
    contributes: number; // recallYes when sampled, else 0
}

// Activation rollup from the nested activation run.json["activation"].
// score = mean over the FULL skill catalog of recall.yes, where a skill counts 0
// unless it ran >= minPrompts positive prompts — so coverage gaps drag it down.
export interface ActivationScore {
    score: number;
    denominator: number;
    minPrompts: number;
    nSkillsSampled: number;
    nCases: number | null;
    perSkill: ActivationSkillScore[];
}

// One activation case for the /runs/<id>/activation cases table. Read from the
// nested activation run.json's task_results, enriched dashboard-side.
export interface ActivationCaseRow {
    taskId: string;
    // The prompt text the agent received; null on rows the enrichment couldn't
    // resolve (e.g. re-merged old run with no per-case task.json).
    prompt: string | null;
    // The skill the prompt targets; null for a negative case (nothing should fire).
    expectedSkill: string | null;
    // The skill that actually fired; "" = none fired, null = unknown (no signal).
    // A mismatch with expectedSkill is a mistake (miss / false positive / wrong skill).
    triggeredSkill: string | null;
}

function mapActivation(
    a: RawActivation | undefined | null,
): ActivationScore | null {
    if (!a || typeof a.score !== "number") return null;
    return {
        score: a.score,
        denominator: a.denominator ?? 0,
        minPrompts: a.min_prompts ?? 20,
        nSkillsSampled: a.n_skills_sampled ?? 0,
        nCases: a.n_cases ?? null,
        perSkill: (a.per_skill ?? []).map((p) => ({
            skill: p.skill ?? "",
            recallYes: p.recall_yes ?? null,
            nPrompts: p.n_prompts ?? 0,
            sampled: p.sampled ?? false,
            contributes: p.contributes ?? 0,
        })),
    };
}

// Components captured in run env_info. Each entry may accept multiple keys —
// the CLI shipped as `cli_git_commit` (when built from source) until
// coder_eval #276 switched the runner to consume @uipath/cli@alpha from
// GitHub Packages, after which it's `cli_version` (output of `uip --version`,
// e.g. "0.1.21-alpha.234"). New keys come first so they win when both
// happen to be present. `nonShaUrl` is the link target used when the env
// value isn't a SHA (e.g. an npm package version) — null means no link.
const COMPONENTS: {
    display: string;
    repo: string | null;
    nonShaUrl: string | null;
    // Link target for a semver-shaped value, given the value itself. Takes
    // precedence over `nonShaUrl` when it returns a URL. This is how a released
    // component links at its own version rather than at a moving list page.
    versionUrl?: (version: string) => string | null;
    keys: string[];
}[] = [
    {
        display: "coder_eval",
        repo: "UiPath/coder_eval",
        nonShaUrl: null,
        // Releases are tagged `v<version>` by the Release workflow, so the
        // version resolves to an exact release page. Runs from before tagging
        // began (v0.8.2) recorded a hardcoded `0.1.0` that was never released;
        // render those as plain text rather than a link that 404s.
        versionUrl: (v) =>
            v === "0.1.0"
                ? null
                : `https://github.com/UiPath/coder_eval/releases/tag/v${v}`,
        // The framework identifies itself by the released package version
        // (env_info `coder_eval`, e.g. "0.9.1"), which is what the suite
        // actually pins — so that is the chip, and it links to the release.
        // `git_commit` is a fallback for two cases the version can't cover: an
        // editable checkout between releases, and legacy runs written before the
        // version was captured. It is a SHA, so it links to the tree.
        keys: ["coder_eval", "git_commit"],
    },
    {
        display: "skills",
        repo: "UiPath/skills",
        nonShaUrl: null,
        keys: ["skills_git_commit"],
    },
    {
        display: "cli",
        repo: "UiPath/cli",
        nonShaUrl: "https://github.com/UiPath/cli/pkgs/npm/cli/versions",
        keys: ["cli_version", "cli_git_commit"],
    },
];

export function extractComponentShas(
    env: RawRunJson["environment_info"],
): ComponentSha[] {
    if (!env) return [];
    const out: ComponentSha[] = [];
    for (const comp of COMPONENTS) {
        let value: string | null = null;
        for (const k of comp.keys) {
            const v = env[k];
            // Skip "unknown": the git-SHA components (coder_eval / skills)
            // resolve to that string when env_info is captured in-container
            // (the task sandbox has no repo checkout to `git rev-parse`), and
            // an unknown SHA is never worth a chip. npm-based versions
            // (cli / *-tool) still resolve in-container, so those survive.
            if (typeof v === "string" && v && v.toLowerCase() !== "unknown") {
                value = v;
                break;
            }
        }
        if (value == null) continue;
        let url: string | null = null;
        if (comp.repo && SHA_RE.test(value)) {
            url = `https://github.com/${comp.repo}/tree/${value}`;
        } else {
            // Not SHA-shaped, so it's a version label. Prefer an exact
            // per-version link; fall back to the component's list page.
            url = comp.versionUrl?.(value) ?? comp.nonShaUrl;
        }
        out.push({ name: comp.display, sha: value, url });
    }
    // Tool plugins (e.g. maestro-tool) version independently of the cli
    // shell, and the plugin's bundled schema — not the shell version — often
    // decides behavior; surface each one as its own chip after `cli`.
    // Published from the same GitHub Packages feed as @uipath/cli.
    const plugins = env.tool_plugins;
    if (plugins != null && typeof plugins === "object") {
        for (const [name, version] of Object.entries(plugins).sort(([a], [b]) =>
            a.localeCompare(b),
        )) {
            if (typeof version !== "string" || !version) continue;
            out.push({
                name,
                sha: version,
                url: `https://github.com/UiPath/cli/pkgs/npm/${encodeURIComponent(name)}/versions`,
            });
        }
    }
    return out;
}

// ---------- Readers ----------

export async function listRunIds(
    source: Source = DEFAULT_SOURCE,
): Promise<string[]> {
    // Local mode resolves here rather than inside blob.ts because RUNS_DIR — and
    // therefore the per-source root — lives in this module. Listing off the bare
    // LOCAL_RUNS_DIR would return the DEFAULT source's ids for every source,
    // while every read below resolves under runsDirFor(RUNS_DIR, source): the
    // listing and the reads would disagree about which container they describe.
    if (LOCAL_RUNS_DIR) return listRunIdsLocal(runsDirFor(RUNS_DIR, source));
    return listRunIdsRemote(source.container);
}

export async function latestRunId(
    source: Source = DEFAULT_SOURCE,
): Promise<string | null> {
    const ids = await listRunIds(source);
    return ids[0] ?? null;
}

async function readJson<T>(p: string): Promise<T | null> {
    try {
        const raw = await fs.readFile(p, "utf-8");
        return JSON.parse(raw) as T;
    } catch {
        return null;
    }
}

async function readRunJson(
    id: string,
    source: Source = DEFAULT_SOURCE,
): Promise<RawRunJson | null> {
    const dir = runsDirFor(RUNS_DIR, source);
    await ensureRunSummary(source.container, id, dir);
    return readJson<RawRunJson>(path.join(dir, id, "run.json"));
}

// The activation suite is a nested sub-run: its self-contained run.json (enriched
// cases + the per-skill rollup) lives at <id>/activation/run.json, separate from
// the skills run.json so the latter stays exactly what coder-eval wrote. Null when
// the run has no activation suite.
async function readActivationRunJson(
    id: string,
    source: Source = DEFAULT_SOURCE,
): Promise<RawRunJson | null> {
    const dir = runsDirFor(RUNS_DIR, source);
    await ensureActivationSummary(source.container, id, dir);
    return readJson<RawRunJson>(path.join(dir, id, "activation", "run.json"));
}

// Activation cases run in the nested sub-run, so their per-case dirs (and row
// data) come from <id>/activation/... rather than the top-level run. coder-eval
// names the activation task "skill-activation", so its case task_ids are
// "skill-activation/<row>".
function isActivationTaskId(taskId: string): boolean {
    return taskId.startsWith("skill-activation/");
}

// Filesystem base for a task's content (before the optional `00` replicate dir):
// activation cases under <id>/activation/default/<taskId>, skills tasks under
// <id>/<variantId>/<taskId>.
//
// `variantId` is the experiment arm — coder_eval's path_utils.build_task_run_dir
// writes <run_dir>/<variant_id>/<task_id>/<NN>/ for every run, using "default"
// when the experiment declares no variants. Defaulting here therefore reproduces
// the old hardcoded path exactly for every single-arm run.
function taskContentBase(
    runId: string,
    taskId: string,
    variantId: string = DEFAULT_VARIANT_ID,
    source: Source = DEFAULT_SOURCE,
): string {
    const dir = runsDirFor(RUNS_DIR, source);
    // The activation sub-run is a nested single-variant run, so its cases always
    // sit under `default` regardless of the outer run's arms.
    return isActivationTaskId(taskId)
        ? path.join(dir, runId, "activation", DEFAULT_VARIANT_ID, taskId)
        : path.join(dir, runId, variantId, taskId);
}

// Resolve the skill (primary grouping axis) for a task. Two-stage fallback:
//   1. New runs: parse "tasks/<skill>/..." from task_path — the folder a task
//      lives under in the skills repo is the authoritative skill name.
//   2. Older runs (or any task without a recognisable task_path): pick the
//      first skill-shaped tag. Convention in the skills repo: every task
//      lists its folder name as the first tag, and skill folders are
//      either "activation" or "uipath-*". The prefix check filters out
//      generic tags like "smoke"/"e2e" that may appear if a task is
//      missing the convention.
// Returns null when neither signal yields anything; the UI buckets these
// under an "unknown" group.
function deriveSkill(
    taskPath: string | null | undefined,
    tags: string[] | null | undefined,
): string | null {
    if (taskPath) {
        const m = taskPath.match(/(?:^|\/)tasks\/([^/]+)\//);
        if (m) return m[1];
    }
    for (const t of tags ?? []) {
        if (t === "activation" || t.startsWith("uipath-")) return t;
    }
    return null;
}

export function toTaskRow(t: RawTaskResult): TaskResultSummary {
    const tags = t.tags ?? [];
    return {
        taskId: t.task_id ?? "",
        variantId: t.variant_id ?? null,
        replicateIndex: t.replicate_index ?? null,
        status: t.status ?? null,
        weightedScore: t.weighted_score ?? null,
        durationSeconds: t.duration ?? null,
        totalCostUsd: t.total_cost_usd ?? null,
        actualCommands: t.actual_commands ?? null,
        totalTurns: t.total_turns ?? null,
        expectedTurns: t.expected_turns ?? null,
        expectedSeconds: t.expected_seconds ?? null,
        hasFinalReply: t.has_final_reply ?? false,
        inputTokens: t.input_tokens ?? null,
        outputTokens: t.output_tokens ?? null,
        cacheCreationTokens: t.cache_creation_input_tokens ?? null,
        cacheReadTokens: t.cache_read_input_tokens ?? null,
        model: t.model_used ?? null,
        tags,
        skill: deriveSkill(t.task_path, tags),
        matureSkipped: t.mature_skipped ?? false,
    };
}

export async function readRunSummary(
    id: string,
    source: Source = DEFAULT_SOURCE,
): Promise<RunSummary | null> {
    const data = await readRunJson(id, source);
    if (!data) return null;
    const taskResults = data.task_results ?? [];
    const totalCost = taskResults.reduce(
        (a, t) => a + (t.total_cost_usd ?? 0),
        0,
    );
    // Sum of per-task durations (compute time). Only use the sum when every
    // task has a duration recorded; otherwise the partial sum would understate
    // the run drastically (e.g. 1/50 tasks with a duration would render as that
    // single task's time). Fall back to wall-clock in that case.
    const taskDurationSum = taskResults.reduce(
        (a, t) => a + (t.duration ?? 0),
        0,
    );
    const allHaveDuration =
        taskResults.length > 0 &&
        taskResults.every((t) => t.duration != null);
    const taskDurationSeconds = allHaveDuration
        ? taskDurationSum
        : (data.total_duration_seconds ?? null);
    const models = tallyModels(taskResults);
    return {
        id,
        startTime: data.start_time ?? null,
        endTime: data.end_time ?? null,
        taskDurationSeconds,
        tasksRun: data.tasks_run ?? taskResults.length,
        tasksSucceeded: data.tasks_succeeded ?? 0,
        tasksFailed: data.tasks_failed ?? 0,
        tasksError: data.tasks_error ?? 0,
        tasksNotGraded: data.tasks_not_graded ?? 0,
        totalCostUsd: taskResults.length ? totalCost : null,
        componentShas: extractComponentShas(data.environment_info),
        harness: extractRunConfig(data).harness,
        model: models.dominant,
        modelCount: models.distinct,
    };
}

// Dominant `model_used` across a run's rows, plus how many distinct models
// appeared. Mirrors mostCommonAgentType's "the thing these tasks ran on" vote,
// but also reports the spread so the run header can say when there was more than
// one instead of silently picking a winner.
export function tallyModels(rows: RawTaskResult[]): {
    dominant: string | null;
    distinct: number;
} {
    const counts = new Map<string, number>();
    for (const r of rows) {
        const m = r.model_used;
        if (typeof m === "string" && m) counts.set(m, (counts.get(m) ?? 0) + 1);
    }
    let dominant: string | null = null;
    let bestN = 0;
    for (const [model, n] of counts) {
        if (n > bestN) {
            dominant = model;
            bestN = n;
        }
    }
    return { dominant, distinct: counts.size };
}

export async function readRunTasks(
    id: string,
    source: Source = DEFAULT_SOURCE,
): Promise<TaskResultSummary[] | null> {
    const data = await readRunJson(id, source);
    if (!data) return null;
    return (data.task_results ?? []).filter((t) => t.task_id).map(toTaskRow);
}

// How many earlier runs to scan when resolving where a mature-skipped task last
// actually executed. A mature task is re-validated on a fixed 1-in-SLOT_COUNT
// slot (SLOT_COUNT = 5 in the runner's maturity.py), so its most recent real
// execution is at most ~5 *canonical* runs back; the headroom absorbs ad-hoc /
// smoke runs that listRunIds() interleaves but maturity replay ignores. The scan
// short-circuits as soon as every task is resolved, so this is a safety cap, not
// the typical read count.
const MATURE_SOURCE_LOOKBACK = 20;

// For each mature-skipped task in `fromRunId`, find the most recent *earlier* run
// whose row for that task actually executed (i.e. is not itself mature-skipped).
// Returns a map taskId → runId; a task with no executed run inside the look-back
// window is simply absent (the grid then renders it non-clickable, as before).
// Reads one run.json per scanned run, newest-first, stopping early once every
// task is resolved. `deps` is a test seam — production uses the real readers.
export async function findMatureSourceRuns(
    matureTaskIds: string[],
    fromRunId: string,
    deps?: {
        listIds?: () => Promise<string[]>;
        readRun?: (id: string) => Promise<RawRunJson | null>;
    },
    source: Source = DEFAULT_SOURCE,
): Promise<Record<string, string>> {
    const out: Record<string, string> = {};
    if (matureTaskIds.length === 0) return out;
    const listIds = deps?.listIds ?? (() => listRunIds(source));
    const readRun = deps?.readRun ?? ((id: string) => readRunJson(id, source));

    // listRunIds() is newest-first; scan strictly-older runs (those after the
    // source's index) so the first executed row we hit is the most recent one.
    const ids = await listIds();
    const start = ids.indexOf(fromRunId);
    if (start < 0) return out;

    const unresolved = new Set(matureTaskIds);
    const limit = Math.min(ids.length, start + 1 + MATURE_SOURCE_LOOKBACK);
    for (let i = start + 1; i < limit && unresolved.size > 0; i++) {
        const data = await readRun(ids[i]);
        for (const t of data?.task_results ?? []) {
            const tid = t.task_id;
            if (!tid || !unresolved.has(tid) || t.mature_skipped) continue;
            out[tid] = ids[i];
            unresolved.delete(tid);
        }
    }
    return out;
}

// Run-level activation rollup — read from the nested activation sub-run's
// run.json. Null when the run has no activation suite. Feeds the run-page card
// and the activation page's score header.
export async function readActivationScore(
    id: string,
    source: Source = DEFAULT_SOURCE,
): Promise<ActivationScore | null> {
    const data = await readActivationRunJson(id, source);
    return mapActivation(data?.activation);
}

// Activation cases — the per-case list for the /runs/<id>/activation page. Read
// from the nested activation run.json's task_results (separate from the skills
// run entirely). Returns the enriched case shape (prompt / skill / triggered)
// the dashboard bakes onto each row.
export async function readActivationTasks(
    id: string,
    source: Source = DEFAULT_SOURCE,
): Promise<ActivationCaseRow[] | null> {
    const data = await readActivationRunJson(id, source);
    if (!data) return null;
    return (data.task_results ?? [])
        .filter((t) => t.task_id)
        .map((t) => ({
            taskId: t.task_id ?? "",
            prompt: t.prompt ?? null,
            // Preserve "" (negative case → "none" expected) vs null (unknown /
            // un-enriched row → "—"). Same for triggeredSkill ("" = nothing fired).
            expectedSkill:
                typeof t.expected_skill === "string" ? t.expected_skill : null,
            triggeredSkill: t.triggered_skill ?? null,
        }));
}

// Minimal per-task / per-run projection used by the front-page overview
// (daily success chart + tag rails). Per-task detail is needed so that a
// tag filter can scope success rate to only matching tasks within a run.
export interface RunOverviewTask {
    taskId: string;
    status: string | null;
    tags: string[];
    skill: string | null;
    totalCostUsd: number | null;
    durationSeconds: number | null;
    weightedScore: number | null;
    actualCommands: number | null;
    totalTurns: number | null;
    expectedTurns: number | null;
    expectedSeconds: number | null;
    visibleTurns: number | null;
    hasFinalReply: boolean;
    // True when the nightly skipped this mature task and carried it forward as a
    // pass (run.json `mature_skipped`). It still counts as a SUCCESS outcome,
    // but it wasn't executed — so its 0-cost/0-duration row must be excluded
    // from per-task averages (see lib/trends.ts) rather than dragging them down.
    // Optional so test factories that predate the field stay valid (undefined
    // === a normal executed row).
    matureSkipped?: boolean;
}

export interface RunOverview {
    id: string;
    tasks: RunOverviewTask[];
    // Whole-run aggregates (mirrors readRunSummary). Carried here so the
    // front-page table and the chart can be built from a single read.
    totalCostUsd: number | null;
    taskDurationSeconds: number | null;
    componentShas: ComponentSha[];
    // Run-level harness (coder-eval AgentKind) from the RunConfig stamp
    // (environment_info.run_config), falling back to the most common per-task
    // agent_config.type. `environment` (alpha/prod) is captured but intentionally
    // NOT surfaced in the UI yet. Both optional so test factories predating them stay valid.
    harness?: string | null;
    environment?: string | null;
    // run.json `start_time` (ISO wall-clock). Pipeline runs derive their date
    // from the date-shaped id; ad-hoc ids carry no date, so the ad-hoc listing
    // orders by this instead. Optional so test factories predating it stay valid.
    startedAt?: string | null;
    // Seconds of every task that ran, over the number that passed — the headline
    // efficiency number, as stamped by the runner. Failures are in the numerator
    // on purpose: a run that spends an hour failing is a worse run. Null when the
    // run predates stamping (the front page then falls back to the task rows).
    timePerPassedTask?: number | null;
}

// Visible-turn count for a task row: the persisted `visible_turns` field when
// present, else reconstructed as actual_commands + (1 if final reply). That
// reconstruction is the documented turn rule (tool calls + final reply) and is
// provably identical to the persisted field — so it backfills the "within
// expected turns" metric for runs written before `visible_turns` existed. null
// when neither signal is present.
export function visibleTurnsFromRaw(t: {
    visible_turns?: number | null;
    actual_commands?: number | null;
    has_final_reply?: boolean;
}): number | null {
    if (t.visible_turns != null) return t.visible_turns;
    if (t.actual_commands != null) {
        return t.actual_commands + (t.has_final_reply ? 1 : 0);
    }
    return null;
}

// The run's harness + environment. Prefers the run-level RunConfig stamped by
// eval_runner into environment_info.run_config; falls back to the most common
// per-task agent_config.type (direct coder-eval / legacy runs carry no stamp).
// Returns null harness when nothing identifies it (legacy pre-harness runs).
export function extractRunConfig(data: RawRunJson): {
    harness: string | null;
    environment: string | null;
} {
    const rc = data.environment_info?.run_config;
    if (rc && typeof rc === "object" && !Array.isArray(rc)) {
        const r = rc as Record<string, unknown>;
        return {
            // A present-but-partial stamp (run_config object with no/empty
            // harness) falls back to the per-task agent_config vote rather
            // than nulling out — otherwise a real codex/antigravity run is
            // mislabeled as the claude-code default.
            harness:
                typeof r.harness === "string" && r.harness
                    ? r.harness
                    : mostCommonAgentType(data.task_results ?? []),
            environment:
                typeof r.environment === "string" ? r.environment : null,
        };
    }
    return {
        harness: mostCommonAgentType(data.task_results ?? []),
        environment: null,
    };
}

// Most frequent per-task agent_config.type across a run's rows, or null if none
// declare one. A run is single-harness in practice, so this is just a robust
// "the harness these tasks ran on" for runs without the run-level stamp.
function mostCommonAgentType(rows: RawTaskResult[]): string | null {
    const counts = new Map<string, number>();
    for (const r of rows) {
        const t = r.agent_config?.type;
        if (typeof t === "string" && t)
            counts.set(t, (counts.get(t) ?? 0) + 1);
    }
    let best: string | null = null;
    let bestN = 0;
    for (const [t, n] of counts) {
        if (n > bestN) {
            best = t;
            bestN = n;
        }
    }
    return best;
}

export async function readRunOverview(
    id: string,
    source: Source = DEFAULT_SOURCE,
): Promise<RunOverview | null> {
    const data = await readRunJson(id, source);
    if (!data) return null;
    const taskResults = data.task_results ?? [];
    const tasks: RunOverviewTask[] = taskResults
        .filter((t) => t.task_id)
        .map((t) => {
            const tags = t.tags ?? [];
            return {
                taskId: t.task_id ?? "",
                status: t.status ?? null,
                tags,
                skill: deriveSkill(t.task_path, tags),
                totalCostUsd: t.total_cost_usd ?? null,
                durationSeconds: t.duration ?? null,
                weightedScore: t.weighted_score ?? null,
                actualCommands: t.actual_commands ?? null,
                totalTurns: t.total_turns ?? null,
                expectedTurns: t.expected_turns ?? null,
                expectedSeconds: t.expected_seconds ?? null,
                visibleTurns: visibleTurnsFromRaw(t),
                hasFinalReply: t.has_final_reply ?? false,
                matureSkipped: t.mature_skipped ?? false,
            };
        });
    const totalCost = taskResults.reduce(
        (a, t) => a + (t.total_cost_usd ?? 0),
        0,
    );
    const taskDurationSum = taskResults.reduce(
        (a, t) => a + (t.duration ?? 0),
        0,
    );
    const allHaveDuration =
        taskResults.length > 0 &&
        taskResults.every((t) => t.duration != null);
    return {
        id,
        tasks,
        totalCostUsd: taskResults.length ? totalCost : null,
        taskDurationSeconds: allHaveDuration
            ? taskDurationSum
            : (data.total_duration_seconds ?? null),
        componentShas: extractComponentShas(data.environment_info),
        ...extractRunConfig(data),
        startedAt: data.start_time ?? null,
        timePerPassedTask: data.timing?.time_per_passed_task ?? null,
    };
}

// Files matching any pattern are hidden from the Artifacts list — they're
// reconstructible build artifacts, local state, or secrets, not deliverables.
// Keep in sync with _EXCLUDE_PATTERNS in dashboard/src/dashboard/blob.py: the
// upload filter and this display filter must agree on what counts as noise.
export const ARTIFACT_EXCLUDE_PATTERNS = [
    "*/.venv/*",
    "*/__pycache__/*",
    "*.pyc",
    "*/bin/*",
    "*/obj/*",
    "*.dll",
    "*.nupkg",
    "*.pdb",
    "*/node_modules/*",
    "*/.npm-prefix/*",
    "*.lock",
    "*.db",
    "*.db-wal",
    "*.db-shm",
    "*.env",
];

// fnmatch semantics (mirrors Python's fnmatch in blob.py): `*` matches any run
// of characters including `/`, `?` matches one. Everything else is literal.
function globToRegExp(glob: string): RegExp {
    const escaped = glob.replace(/[.+^${}()|[\]\\]/g, "\\$&");
    return new RegExp(`^${escaped.replace(/\*/g, ".*").replace(/\?/g, ".")}$`);
}

const EXCLUDE_RES = ARTIFACT_EXCLUDE_PATTERNS.map(globToRegExp);

export function isExcludedArtifact(relPath: string): boolean {
    return EXCLUDE_RES.some((re) => re.test(relPath));
}

function artifactRank(a: ArtifactRef): number {
    const base = (a.relPath.split("/").pop() ?? a.relPath).toLowerCase();
    return DELIVERABLE_KINDS.has(a.kind) || DELIVERABLE_NAMES.has(base) ? 0 : 1;
}

// Deliverables first, then shallower paths (surfaces root-level sdd.md /
// recommendation.json above deep fixture/scaffolding trees), then alpha.
export function sortArtifacts(artifacts: ArtifactRef[]): ArtifactRef[] {
    return [...artifacts].sort((x, y) => {
        const r = artifactRank(x) - artifactRank(y);
        if (r !== 0) return r;
        const dx = x.relPath.split("/").length;
        const dy = y.relPath.split("/").length;
        if (dx !== dy) return dx - dy;
        return x.relPath.localeCompare(y.relPath);
    });
}

export async function walkArtifacts(
    root: string,
    prefix = "",
): Promise<ArtifactRef[]> {
    const out: ArtifactRef[] = [];
    const entries = await fs
        .readdir(root, { withFileTypes: true })
        .catch(() => []);
    for (const e of entries) {
        const full = path.join(root, e.name);
        const rel = prefix ? `${prefix}/${e.name}` : e.name;
        // Skip symlinks: they're harness scaffolding, not deliverables. codex
        // runs symlink every installed skill into `.agents/skills/uipath-*` so
        // the agent can read SKILL.md off disk (no native Skill tool) — 20 per
        // task, pure noise in the list. Skipping also avoids descending a
        // symlinked dir and enumerating its target's contents.
        if (e.isSymbolicLink()) continue;
        if (e.isDirectory()) {
            // Prune whole dirs (.venv, node_modules, ...) via a probe path so
            // the `*/dir/*` patterns alone decide what to descend into — no
            // hardcoded dir names that could drift from the exclude list.
            if (isExcludedArtifact(`${rel}/_`)) continue;
            out.push(...(await walkArtifacts(full, rel)));
        } else {
            if (isExcludedArtifact(rel)) continue;
            const ext = path.extname(e.name).toLowerCase().replace(/^\./, "");
            const stat = await fs.stat(full).catch(() => null);
            out.push({
                relPath: rel,
                kind: ext || "file",
                sizeBytes: stat?.size ?? 0,
            });
        }
    }
    return out;
}

// ---------- Parsers ----------

function extractBalancedJson(
    text: string,
    openIdx: number,
): string | null {
    let depth = 0;
    let inStr = false;
    let escape = false;
    for (let i = openIdx; i < text.length; i++) {
        const ch = text[i];
        if (escape) {
            escape = false;
            continue;
        }
        if (inStr) {
            if (ch === "\\") escape = true;
            else if (ch === '"') inStr = false;
            continue;
        }
        if (ch === '"') inStr = true;
        else if (ch === "{") depth++;
        else if (ch === "}") {
            depth--;
            if (depth === 0) return text.slice(openIdx, i + 1);
        }
    }
    return null;
}

function previewGlobal(val: unknown): string | null {
    if (val == null) return null;
    if (typeof val === "string") return val.length > 200 ? val.slice(0, 200) + "…" : val;
    if (typeof val === "number" || typeof val === "boolean") return String(val);
    try {
        const s = JSON.stringify(val);
        return s.length > 200 ? s.slice(0, 200) + "…" : s;
    } catch {
        return null;
    }
}

function parseFlowDebug(
    criteria: CriterionResult[],
): FlowDebugResult | null {
    const marker = '"Code": "FlowDebug"';
    type Parsed = {
        Code?: string;
        Data?: {
            finalStatus?: string;
            studioWebUrl?: string;
            jobKey?: string;
            elementExecutions?: Array<{
                elementId?: string;
                elementType?: string;
                status?: string;
                startedAt?: string;
                completedAt?: string;
                error?: { message?: string };
            }>;
            variables?: {
                globals?: Record<string, unknown>;
            };
        };
    };
    // FlowDebug payloads are captured verbatim in the details string of the
    // run_command success criterion that invokes `uip maestro flow debug`. Walk
    // backwards from the marker to find the enclosing JSON object, tolerating
    // whatever stdout/stderr prefix the criterion wraps around it.
    for (const c of criteria) {
        const details = c.details ?? "";
        const markerIdx = details.indexOf(marker);
        if (markerIdx === -1) continue;
        for (
            let head = details.lastIndexOf("{", markerIdx);
            head !== -1;
            head = details.lastIndexOf("{", head - 1)
        ) {
            const blob = extractBalancedJson(details, head);
            if (!blob) continue;
            let parsed: Parsed;
            try {
                parsed = JSON.parse(blob) as Parsed;
            } catch {
                continue;
            }
            if (parsed.Code !== "FlowDebug") continue;
            const data = parsed.Data ?? {};
            const globals = data.variables?.globals ?? {};
            const elements: ElementExecution[] = (
                data.elementExecutions ?? []
            ).map((e) => {
                const id = e.elementId ?? "?";
                const outputKey =
                    `${id}.output` in globals
                        ? `${id}.output`
                        : Object.keys(globals).find((k) =>
                            k.startsWith(`${id}.`),
                        );
                const outputVal = outputKey ? globals[outputKey] : null;
                return {
                    elementId: id,
                    elementType: e.elementType ?? null,
                    status: e.status ?? "Unknown",
                    startedAt: e.startedAt ?? null,
                    completedAt: e.completedAt ?? null,
                    errorMessage: e.error?.message ?? null,
                    outputPreview:
                        outputVal != null ? previewGlobal(outputVal) : null,
                };
            });
            return {
                finalStatus: data.finalStatus ?? null,
                studioWebUrl: data.studioWebUrl ?? null,
                jobKey: data.jobKey ?? null,
                elements,
            };
        }
    }
    return null;
}

interface CommandEntry {
    tool_name?: string;
    tool_id?: string;
    parameters?: Record<string, unknown>;
    duration_ms?: number;
    result_status?: string;
    result_summary?: unknown;
    error_message?: string | null;
    // Approx token size of the tool result the model received, measured from the
    // untruncated result content (computed field on CommandTelemetry). A direct,
    // cache-independent "tool output size" — present whether caching was on/off.
    result_tokens?: number | null;
}

interface ContentBlockEntry {
    block_type?: "thinking" | "tool_use" | "text";
    text?: string | null;
    thinking?: string | null;
    tool_use_id?: string | null;
    is_error?: boolean;
}

interface MessageEntry {
    role?: string;
    started_at?: string | null;
    completed_at?: string | null;
    generation_duration_ms?: number | null;
    content_blocks?: ContentBlockEntry[];
    // Anthropic API message_id. The Claude Code CLI splits one API response
    // into per-block-kind events that share this id; we collapse by id when
    // present, falling back to a wall-clock gap heuristic for older runs that
    // didn't record it.
    message_id?: string | null;
    // The Task tool_use_id that spawned this message's sub-agent, or null for
    // the main thread. KEY ABSENT (undefined) on runs that predate branch
    // capture — distinct from an explicit null. Used to model the cache cascade
    // as a tree; its absence disables the cost simulator (we can't tell whether
    // sub-agents ran, so a flat cascade would be wrong).
    parent_tool_use_id?: string | null;
    // Per-message token usage (recorded since coder_eval #336). Absent on
    // legacy runs. cache_* keys here are the message-record names, distinct
    // from the iteration token_usage's cache_*_input_tokens.
    input_tokens?: number | null;
    output_tokens?: number | null;
    cache_creation_tokens?: number | null;
    cache_read_tokens?: number | null;
    reasoning_tokens?: number | null;
    model?: string | null;
    // Only on a role="reconciliation" entry: why these tokens are unattributed.
    note?: string | null;
}

// One raw per-call audit row on a turn's `provider_call_costs` list. Snake-case
// as serialized in task.json; mapped to ProviderCallEntry (camelCase).
interface ProviderCallEntryRaw {
    call_id?: string | null;
    cost_usd?: number | null;
    input_tokens?: number | null;
    cache_read_tokens?: number | null;
    cache_write_tokens?: number | null;
    output_tokens?: number | null;
}

export interface TurnEntry {
    commands?: CommandEntry[];
    messages?: MessageEntry[];
    // Model the turn ran on (iteration `model_used`). Used to price the synthetic
    // reconciliation row, which carries no model of its own.
    model_used?: string | null;
    token_usage?: TokenUsageEntry | null;
    // Per-call actual cost + cache audit rows (LiteLLM/open-weight backend);
    // empty/absent on Claude/Bedrock. Surfaced as a standalone per-call table.
    provider_call_costs?: ProviderCallEntryRaw[];
    result_summary?: {
        result?: string | null;
        stop_reason?: string | null;
    } | null;
}

interface TokenUsageEntry {
    // On current runs `input_tokens` is the TOTAL prompt input (uncached +
    // cache-creation + cache-read); `uncached_input_tokens` is the disjoint
    // fresh slice we actually want to display next to the cache columns. Legacy
    // runs predate the split and carry only `input_tokens`, which back then
    // meant the disjoint uncached slice — so it's the correct fallback.
    input_tokens?: number | null;
    uncached_input_tokens?: number | null;
    output_tokens?: number | null;
    cache_creation_input_tokens?: number | null;
    cache_read_input_tokens?: number | null;
    total_cost_usd?: number | null;
}

// Token counts summed across all iterations of a task. Optional so legacy
// task.json files without per-iteration token_usage just expose zeros.
export interface TokenTotals {
    input: number;
    output: number;
    cacheCreation: number;
    cacheRead: number;
    total: number;
}

function summarizeCommand(cmd: CommandEntry): string {
    const p = cmd.parameters ?? {};
    // Prefer the first human-readable field the tool provides, in priority
    // order. Falls back to a stringified params object if none match.
    const keys = [
        "description",
        "command",
        "file_path",
        "skill",
        "query",
        "pattern",
    ];
    for (const k of keys) {
        const v = p[k];
        if (typeof v === "string" && v.length > 0) {
            const flat = v.replace(/[\n\t]+/g, " ").trim();
            return flat.length > 140 ? flat.slice(0, 137) + "…" : flat;
        }
    }
    let s: string;
    try {
        s = JSON.stringify(p);
    } catch {
        return "";
    }
    return s.length > 140 ? s.slice(0, 137) + "…" : s;
}

// Cap message-level previews to keep payloads sane on long-running tasks.
// Result previews are stored short anyway (the raw task.json truncates them
// at ~200 chars for most non-Bash tools); we just need a deterministic ceiling.
const RESULT_PREVIEW_CAP = 280;
const TEXT_PREVIEW_CAP = 600;
const THINKING_PREVIEW_CAP = 800;

// Pick the operative arg the agent actually ran with. For Bash that's the
// command string; for Read/Write/Edit it's file_path; for Skill it's the skill
// id; for Grep it's pattern; for WebFetch it's url; etc. Falls through to the
// first non-empty string param if no known key matched.
function pickArgText(params: Record<string, unknown>): string | null {
    const keys = [
        "command",
        "file_path",
        "filePath",
        "path",
        "url",
        "skill",
        "pattern",
        "query",
        // Agent (sub-agent) tool: the operative input is the delegation prompt,
        // not the short `description`. Without this the row showed only the
        // one-line description, so a 1000+ char prompt looked like a tiny input
        // next to a large output-token count.
        "prompt",
    ];
    for (const k of keys) {
        const v = params[k];
        if (typeof v === "string" && v.length > 0) return v;
    }
    for (const v of Object.values(params)) {
        if (typeof v === "string" && v.length > 0) return v;
    }
    return null;
}

function pickDescription(params: Record<string, unknown>): string | null {
    const v = params["description"];
    return typeof v === "string" && v.length > 0 ? v : null;
}

function previewString(value: unknown, cap: number): string | null {
    if (value == null) return null;
    let s: string;
    if (typeof value === "string") {
        s = value;
    } else {
        try {
            s = JSON.stringify(value);
        } catch {
            return null;
        }
    }
    s = s.replace(/[\r]+/g, "");
    if (s.length > cap) s = s.slice(0, cap - 1) + "…";
    return s.length > 0 ? s : null;
}

// Gap between one assistant message's completed_at and the next message's
// started_at that we treat as "same model emission". coder_eval records each
// content-block kind (thinking / tool_use / text) as its own MessageEntry,
// so a single conceptual turn often spans 2-3 entries with gap≈0. Tool
// execution sits *between* turns and adds 100ms+, so 100ms is a safe split
// threshold. Anything below is grouped into one logical turn; this is also
// how parallel tool_use blocks (whether one MessageEntry with N tool_use
// blocks, or N MessageEntries emitted instantaneously) get folded together.
const SAME_EMISSION_GAP_MS = 100;

function tsMs(s: string | null | undefined): number | null {
    if (!s) return null;
    const t = Date.parse(s);
    return Number.isNaN(t) ? null : t;
}

// ~4 chars/token is the common rough estimate for English/code. The exact
// constant doesn't matter for *splitting* time across parallel tools (it
// cancels in the ratio), but using a real-ish number keeps the helper useful
// if anyone wants a token estimate independently.
const CHARS_PER_TOKEN = 4;

export function approxTokens(params: Record<string, unknown> | null | undefined): number {
    if (!params) return 0;
    try {
        const s = JSON.stringify(params);
        return Math.ceil(s.length / CHARS_PER_TOKEN);
    } catch {
        return 0;
    }
}

export function parseMessages(turns: TurnEntry[]): MessageEvent[] {
    const out: MessageEvent[] = [];
    let order = 0;
    for (const turn of turns) {
        // Resolve tool_use_id -> CommandEntry per iteration (the id is locally
        // unique). Used to pull execution time + result preview onto tool_use
        // blocks.
        const byToolUseId = new Map<string, CommandEntry>();
        for (const cmd of turn.commands ?? []) {
            if (cmd.tool_id) byToolUseId.set(cmd.tool_id, cmd);
        }

        // First pass: collect assistant messages with parsed block content.
        type Raw = {
            startedAt: string | null;
            completedAt: string | null;
            startMs: number | null;
            endMs: number | null;
            generationMs: number | null;
            messageId: string | null;
            // null = main thread, string = sub-agent branch, undefined = run
            // didn't record branch info (legacy).
            parentToolUseId: string | null | undefined;
            blockTypes: ("thinking" | "tool_use" | "text")[];
            thinkingParts: string[];
            textParts: string[];
            toolUses: MessageToolUse[];
            toolTokenProxies: number[];
            inputTokens: number | null;
            outputTokens: number | null;
            cacheWriteTokens: number | null;
            cacheReadTokens: number | null;
            reasoningTokens: number | null;
            model: string | null;
        };
        const raws: Raw[] = [];
        for (const msg of turn.messages ?? []) {
            if (msg.role !== "assistant") continue;
            const r: Raw = {
                startedAt: msg.started_at ?? null,
                completedAt: msg.completed_at ?? null,
                startMs: tsMs(msg.started_at),
                endMs: tsMs(msg.completed_at),
                generationMs:
                    typeof msg.generation_duration_ms === "number"
                        ? msg.generation_duration_ms
                        : null,
                messageId: typeof msg.message_id === "string" ? msg.message_id : null,
                // Preserve the absent/null distinction: undefined when the run
                // never wrote the key (legacy), null when explicitly main thread.
                parentToolUseId:
                    msg.parent_tool_use_id === undefined
                        ? undefined
                        : typeof msg.parent_tool_use_id === "string"
                          ? msg.parent_tool_use_id
                          : null,
                blockTypes: [],
                thinkingParts: [],
                textParts: [],
                toolUses: [],
                toolTokenProxies: [],
                inputTokens:
                    typeof msg.input_tokens === "number" ? msg.input_tokens : null,
                outputTokens:
                    typeof msg.output_tokens === "number" ? msg.output_tokens : null,
                cacheWriteTokens:
                    typeof msg.cache_creation_tokens === "number"
                        ? msg.cache_creation_tokens
                        : null,
                cacheReadTokens:
                    typeof msg.cache_read_tokens === "number"
                        ? msg.cache_read_tokens
                        : null,
                reasoningTokens:
                    typeof msg.reasoning_tokens === "number"
                        ? msg.reasoning_tokens
                        : null,
                model: typeof msg.model === "string" ? msg.model : null,
            };
            for (const b of msg.content_blocks ?? []) {
                if (b.block_type === "thinking") {
                    r.blockTypes.push("thinking");
                    if (b.thinking) r.thinkingParts.push(b.thinking);
                } else if (b.block_type === "text") {
                    r.blockTypes.push("text");
                    if (b.text) r.textParts.push(b.text);
                } else if (b.block_type === "tool_use") {
                    r.blockTypes.push("tool_use");
                    const id = b.tool_use_id ?? null;
                    const cmd = id ? byToolUseId.get(id) : undefined;
                    const params = cmd?.parameters ?? {};
                    const argText = pickArgText(params);
                    const description = pickDescription(params);
                    // Crude token proxy: serialized argument byte length. Used
                    // below to weight per-tool generation time across parallel
                    // tool_use blocks emitted in one raw.
                    const tokenProxy = approxTokens(params);
                    r.toolTokenProxies.push(tokenProxy);
                    r.toolUses.push({
                        toolName: cmd?.tool_name ?? "unknown",
                        toolUseId: id,
                        summary: cmd ? summarizeCommand(cmd) : "",
                        argText: previewString(argText, 400),
                        description:
                            description && description !== argText
                                ? previewString(description, 200)
                                : null,
                        // genMs set below after we know how many tool_uses share this raw
                        genMs: null,
                        durationMs:
                            typeof cmd?.duration_ms === "number"
                                ? cmd.duration_ms
                                : null,
                        isError:
                            b.is_error === true ||
                            (cmd?.result_status != null &&
                                cmd.result_status !== "success"),
                        resultPreview: previewString(
                            cmd?.result_summary ?? cmd?.error_message ?? null,
                            RESULT_PREVIEW_CAP,
                        ),
                        outputTokens: null,
                        resultTokens:
                            typeof cmd?.result_tokens === "number"
                                ? cmd.result_tokens
                                : null,
                    });
                }
            }
            // Split the raw's generation time across its tool_use blocks,
            // weighted by an approximate token count (param payload size).
            // The SDK only reports one generation_duration_ms per raw, so for
            // parallel tool_uses we attribute proportional to argument size —
            // larger calls plausibly cost more output tokens to generate. Falls
            // back to even-split when all proxies are zero.
            if (r.generationMs != null && r.toolUses.length > 0) {
                const proxies = r.toolTokenProxies;
                const total = proxies.reduce((a, b) => a + b, 0);
                for (let i = 0; i < r.toolUses.length; i++) {
                    const weight =
                        total > 0 ? proxies[i] / total : 1 / r.toolUses.length;
                    r.toolUses[i].genMs = r.generationMs * weight;
                }
            }
            // Per-tool output tokens. The agent records output_tokens per
            // emission, so a tool emission's output_tokens belongs to its
            // tool_use block(s) directly — no gen-time guesswork. Only when a
            // single emission carries multiple parallel tool_uses do we split
            // it (by arg-size proxy, exact remainder on the last tool).
            if (r.outputTokens != null && r.toolUses.length > 0) {
                const proxies = r.toolTokenProxies;
                const total = proxies.reduce((a, b) => a + b, 0);
                let assigned = 0;
                for (let i = 0; i < r.toolUses.length; i++) {
                    if (i === r.toolUses.length - 1) {
                        r.toolUses[i].outputTokens = r.outputTokens - assigned;
                    } else {
                        const weight =
                            total > 0 ? proxies[i] / total : 1 / r.toolUses.length;
                        const share = Math.round(r.outputTokens * weight);
                        r.toolUses[i].outputTokens = share;
                        assigned += share;
                    }
                }
            }
            raws.push(r);
        }

        // Second pass: collapse consecutive raws that share a model emission
        // (tiny gap between prev.end and next.start). Parallel tool_use lives
        // entirely inside one emission, so it ends up in one MessageEvent.
        let group: Raw[] = [];
        const flush = () => {
            if (group.length === 0) return;
            const head = group[0];
            const tail = group[group.length - 1];
            const blockTypes: MessageEvent["blockTypes"] = [];
            const thinkingParts: string[] = [];
            const textParts: string[] = [];
            const toolUses: MessageToolUse[] = [];
            let genSum = 0;
            let haveGen = false;
            // Each raw is one content-block emission (see SAME_EMISSION_GAP_MS
            // comment), so its generation_duration_ms attaches to whichever
            // block kind it carries.
            let thinkSum = 0;
            let haveThink = false;
            let textSum = 0;
            let haveText = false;
            let toolGenSum = 0;
            let haveToolGen = false;
            let inputTokSum = 0;
            let haveInputTok = false;
            let outputTokSum = 0;
            let haveOutputTok = false;
            let cacheWriteSum = 0;
            let haveCacheWrite = false;
            let cacheReadSum = 0;
            let haveCacheRead = false;
            let reasoningSum = 0;
            let haveReasoning = false;
            // Real per-emission output attributed to thinking: the agent
            // distributes a call's output_tokens across its block-emissions by
            // content length, so a thinking-only emission's output_tokens IS
            // the thinking share (reasoning_tokens is ~always 0 from the SDK,
            // so we don't rely on it). Mirrors the gen-time attribution below.
            let thinkingOutSum = 0;
            let haveThinkingOut = false;
            // Likewise for text: a text emission's output_tokens is its share.
            let textOutSum = 0;
            let haveTextOut = false;
            // First non-null model id in the group — consumed by the
            // cascade-aware thinking-cost simulator to price each message.
            let model: string | null = null;
            for (const r of group) {
                blockTypes.push(...r.blockTypes);
                thinkingParts.push(...r.thinkingParts);
                textParts.push(...r.textParts);
                toolUses.push(...r.toolUses);
                if (model == null && r.model != null) model = r.model;
                if (r.generationMs != null) {
                    genSum += r.generationMs;
                    haveGen = true;
                    if (r.blockTypes.includes("thinking")) {
                        thinkSum += r.generationMs;
                        haveThink = true;
                    } else if (r.blockTypes.includes("tool_use")) {
                        toolGenSum += r.generationMs;
                        haveToolGen = true;
                    } else if (r.blockTypes.includes("text")) {
                        textSum += r.generationMs;
                        haveText = true;
                    }
                }
                if (r.inputTokens != null) {
                    inputTokSum += r.inputTokens;
                    haveInputTok = true;
                }
                if (r.outputTokens != null) {
                    outputTokSum += r.outputTokens;
                    haveOutputTok = true;
                    // Attribute the emission's output to its block kind (each
                    // raw is one kind; priority mirrors the gen-time split).
                    // Tool output is attached per-tool in the first pass.
                    if (r.blockTypes.includes("thinking")) {
                        thinkingOutSum += r.outputTokens;
                        haveThinkingOut = true;
                    } else if (r.blockTypes.includes("text")) {
                        textOutSum += r.outputTokens;
                        haveTextOut = true;
                    }
                }
                if (r.cacheWriteTokens != null) {
                    cacheWriteSum += r.cacheWriteTokens;
                    haveCacheWrite = true;
                }
                if (r.cacheReadTokens != null) {
                    cacheReadSum += r.cacheReadTokens;
                    haveCacheRead = true;
                }
                if (r.reasoningTokens != null) {
                    reasoningSum += r.reasoningTokens;
                    haveReasoning = true;
                }
            }
            // Per-block output comes straight from each emission's recorded
            // output_tokens (thinking + text here, tools in the first pass) —
            // the agent already split the call total across blocks by content
            // length, so there's no re-approximation to do. These sum to the
            // group's outputTokens.
            const textOutputTokens = haveTextOut ? textOutSum : null;
            out.push({
                index: ++order,
                role: "assistant",
                startedAt: head.startedAt,
                completedAt: tail.completedAt,
                generationMs: haveGen ? genSum : null,
                thinkingMs: haveThink ? thinkSum : null,
                textMs: haveText ? textSum : null,
                toolGenMs: haveToolGen ? toolGenSum : null,
                blockTypes,
                thinkingText: previewString(
                    thinkingParts.join("\n").trim(),
                    THINKING_PREVIEW_CAP,
                ),
                text: previewString(textParts.join("\n").trim(), TEXT_PREVIEW_CAP),
                toolUses,
                inputTokens: haveInputTok ? inputTokSum : null,
                outputTokens: haveOutputTok ? outputTokSum : null,
                cacheWriteTokens: haveCacheWrite ? cacheWriteSum : null,
                cacheReadTokens: haveCacheRead ? cacheReadSum : null,
                // All raws in a group are one emission → one branch; head is
                // representative.
                parentToolUseId: head.parentToolUseId,
                reasoningTokens: haveReasoning ? reasoningSum : null,
                thinkingOutputTokens: haveThinkingOut ? thinkingOutSum : null,
                textOutputTokens,
                model,
                // Rate-card estimate for this message (actual per-call cost lives
                // in the separate providerCalls table, not distributed here).
                costUsd: messageCostUsd({
                    model,
                    inputTokens: haveInputTok ? inputTokSum : null,
                    outputTokens: haveOutputTok ? outputTokSum : null,
                    cacheWriteTokens: haveCacheWrite ? cacheWriteSum : null,
                    cacheReadTokens: haveCacheRead ? cacheReadSum : null,
                }),
                note: null,
            });
            group = [];
        };
        for (const r of raws) {
            if (group.length === 0) {
                group.push(r);
                continue;
            }
            const prev = group[group.length - 1];
            // Prefer message_id when both raws have one — that's the
            // authoritative signal that the CLI split a single API response.
            // Fall back to the wall-clock gap heuristic for older runs that
            // didn't record message_id.
            let sameEmission: boolean;
            if (prev.messageId != null && r.messageId != null) {
                sameEmission = prev.messageId === r.messageId;
            } else {
                const gap =
                    prev.endMs != null && r.startMs != null
                        ? r.startMs - prev.endMs
                        : Number.POSITIVE_INFINITY;
                sameEmission = gap <= SAME_EMISSION_GAP_MS;
            }
            if (sameEmission) {
                group.push(r);
            } else {
                flush();
                group.push(r);
            }
        }
        flush();

        // After this turn's assistant emissions, surface the backend's synthetic
        // reconciliation entry (tokens billed but never streamed as a generation)
        // as its own row. Booked per-turn by the EventCollector so the four token
        // buckets across the whole stream sum EXACTLY to the run total — that's
        // what lets the dashboard sum the stream instead of reading a separate
        // aggregate.
        for (const msg of turn.messages ?? []) {
            if (msg.role !== "reconciliation") continue;
            out.push({
                index: ++order,
                role: "reconciliation",
                startedAt: null,
                completedAt: null,
                generationMs: null,
                thinkingMs: null,
                textMs: null,
                toolGenMs: null,
                blockTypes: [],
                thinkingText: null,
                text: null,
                toolUses: [],
                inputTokens: typeof msg.input_tokens === "number" ? msg.input_tokens : null,
                outputTokens: typeof msg.output_tokens === "number" ? msg.output_tokens : null,
                cacheWriteTokens:
                    typeof msg.cache_creation_tokens === "number" ? msg.cache_creation_tokens : null,
                cacheReadTokens: typeof msg.cache_read_tokens === "number" ? msg.cache_read_tokens : null,
                parentToolUseId: null,
                reasoningTokens: null,
                thinkingOutputTokens: null,
                textOutputTokens: null,
                // Price the reconciliation row so summing the Cost column across
                // the stream reproduces the turn total. Critical for providers
                // (e.g. OpenRouter/LiteLLM) whose per-message stream is sparse and
                // dumps most tokens into this one row — leaving it uncosted made
                // the visible per-row costs sum to far less than the real total.
                // Excluded from the cost simulator (by role), and the authoritative
                // task total_cost_usd is unaffected (it reads the backend aggregate,
                // not a sum of per-message costUsd), so there is no double-count.
                model: turn.model_used ?? null,
                // Price the residual tokens from the rate card (Claude/Bedrock).
                // Actual per-call cost (LiteLLM backend) is not distributed onto
                // messages — it lives in the separate providerCalls table.
                costUsd: messageCostUsd({
                    model: turn.model_used ?? null,
                    inputTokens: typeof msg.input_tokens === "number" ? msg.input_tokens : null,
                    outputTokens: typeof msg.output_tokens === "number" ? msg.output_tokens : null,
                    cacheWriteTokens:
                        typeof msg.cache_creation_tokens === "number" ? msg.cache_creation_tokens : null,
                    cacheReadTokens: typeof msg.cache_read_tokens === "number" ? msg.cache_read_tokens : null,
                }),
                note: typeof msg.note === "string" ? msg.note : null,
            });
        }
    }
    return out;
}

// Project each turn's `provider_call_costs` audit rows (LiteLLM/open-weight
// backend) into the per-call table shape, keyed by iteration index. Turns with
// no rows are skipped, so the result is empty on Claude/Bedrock runs and the
// table doesn't render.
export function parseProviderCalls(
    turns: TurnEntry[],
): { iteration: number; calls: ProviderCallEntry[] }[] {
    const out: { iteration: number; calls: ProviderCallEntry[] }[] = [];
    turns.forEach((turn, iteration) => {
        const raw = turn.provider_call_costs ?? [];
        if (raw.length === 0) return;
        const calls: ProviderCallEntry[] = raw.map((c) => ({
            callId: typeof c.call_id === "string" ? c.call_id : null,
            costUsd: typeof c.cost_usd === "number" ? c.cost_usd : null,
            inputTokens: typeof c.input_tokens === "number" ? c.input_tokens : null,
            cacheReadTokens:
                typeof c.cache_read_tokens === "number" ? c.cache_read_tokens : null,
            cacheWriteTokens:
                typeof c.cache_write_tokens === "number" ? c.cache_write_tokens : null,
            outputTokens:
                typeof c.output_tokens === "number" ? c.output_tokens : null,
        }));
        out.push({ iteration, calls });
    });
    return out;
}

function parseToolCalls(turns: TurnEntry[], max = 200): ToolCall[] {
    const out: ToolCall[] = [];
    let i = 0;
    for (const turn of turns) {
        for (const cmd of turn.commands ?? []) {
            if (!cmd.tool_name) continue;
            out.push({
                index: ++i,
                tool: cmd.tool_name,
                summary: summarizeCommand(cmd),
            });
            if (out.length >= max) return out;
        }
    }
    return out;
}

// ---------- Task detail ----------

// Two-digit, zero-padded replicate dir name, mirroring Python's
// replicate_subdir_name (00, 01, …). Repeated runs of one task live in sibling
// <NN> dirs under the task folder.
export function replicateDirName(replicate: number): string {
    return String(replicate).padStart(2, "0");
}

// Runs uploaded before the replicate-index layout (<taskDir>/task.json) and
// after (<taskDir>/<NN>/task.json) coexist in the blob store. Prefer the
// nested replicate shape; fall back to flat ONLY for replicate 0 (the implicit
// single result a legacy run had) so legacy runs keep rendering. A missing
// non-zero replicate resolves to its (absent) nested dir → read fails → 404.
async function resolveTaskContentDir(
    taskDir: string,
    replicate: number,
): Promise<string> {
    const nested = path.join(taskDir, replicateDirName(replicate));
    try {
        await fs.access(path.join(nested, "task.json"));
        return nested;
    } catch {
        return replicate === 0 ? taskDir : nested;
    }
}

// Row matcher shared by every per-task reader: a row belongs to (taskId,
// variantId) iff both match, with a missing variant_id reading as the default
// arm. Keeping it in one place is what stops the two arms of a multi-variant run
// from resolving to each other's transcript.
function rowMatches(
    t: RawTaskResult,
    taskId: string,
    variantId: string,
): boolean {
    return (
        t.task_id === taskId && (t.variant_id ?? DEFAULT_VARIANT_ID) === variantId
    );
}

// The arm a task URL resolves to when it names none. Only `experiments/
// default.yaml` calls its arm `default`; every other experiment names its own
// (`sonnet`/`opus`, `baseline`, `e2e`/`smoke`), so an experiment run has no
// `default` row at all and a bare /runs/<id>/<task> would match nothing and
// 404. Trends, the watchlist and every pre-variant bookmark still emit exactly
// that form, so an unnamed arm resolves to the task's first arm in the order
// the grid lists them. An explicitly named arm is returned untouched — a URL
// asking for an arm the run does not have still 404s downstream rather than
// quietly rendering a different one.
export async function resolveVariantId(
    runId: string,
    taskId: string,
    requested: string | null,
    source: Source = DEFAULT_SOURCE,
): Promise<string> {
    if (requested != null) return requested;
    const data = isActivationTaskId(taskId)
        ? await readActivationRunJson(runId, source)
        : await readRunJson(runId, source);
    const arms = new Set<string>();
    for (const t of data?.task_results ?? []) {
        if (t.task_id === taskId) arms.add(t.variant_id ?? DEFAULT_VARIANT_ID);
    }
    // No rows (unknown task, unreadable run.json) keeps the default arm, so the
    // caller still 404s the way it did before variants were addressable.
    if (arms.size === 0 || arms.has(DEFAULT_VARIANT_ID)) return DEFAULT_VARIANT_ID;
    return [...arms].sort()[0];
}

// Replicate indices present for a task in this run, ascending (e.g. [0, 1, 2]
// for a task run 3×). Drives the task page's run selector. A non-repeated or
// legacy run yields [0] (rows carry no replicate_index → treated as 0); an
// unknown task yields []. Reads the same run.json readTaskDetail matches against.
export async function readTaskReplicates(
    runId: string,
    taskId: string,
    source: Source = DEFAULT_SOURCE,
    variantId: string = DEFAULT_VARIANT_ID,
): Promise<number[]> {
    const data = isActivationTaskId(taskId)
        ? await readActivationRunJson(runId, source)
        : await readRunJson(runId, source);
    const indices = (data?.task_results ?? [])
        .filter((t) => rowMatches(t, taskId, variantId))
        .map((t) => t.replicate_index ?? 0);
    return [...new Set(indices)].sort((a, b) => a - b);
}

export async function readTaskDetail(
    runId: string,
    taskId: string,
    replicate = 0,
    source: Source = DEFAULT_SOURCE,
    variantId: string = DEFAULT_VARIANT_ID,
): Promise<TaskDetail | null> {
    const dir = runsDirFor(RUNS_DIR, source);
    await ensureTaskDir(source.container, runId, taskId, dir, variantId);

    // Activation cases live in the nested activation sub-run; skills tasks in the
    // top-level run. Read the row from whichever run.json owns this task so the
    // trace (linked from the activation page) still resolves.
    const data = isActivationTaskId(taskId)
        ? await readActivationRunJson(runId, source)
        : await readRunJson(runId, source);
    // A run with variants repeats a task_id once per arm and a repeated run
    // repeats it once per replicate, so the row is only identified by all three
    // of (task_id, variant_id, replicate_index). Legacy rows carry neither
    // variant_id nor replicate_index (null) → treated as the default arm at
    // replicate 0, so an old single-result run still resolves.
    const matches = (data?.task_results ?? []).filter((t) =>
        rowMatches(t, taskId, variantId),
    );
    const rawTask =
        matches.find((t) => (t.replicate_index ?? 0) === replicate) ??
        (replicate === 0 ? matches[0] : undefined);
    if (!rawTask) return null;
    const row = toTaskRow(rawTask);

    const taskDir = taskContentBase(runId, taskId, variantId, source);
    const contentDir = await resolveTaskContentDir(taskDir, replicate);
    const task = await readJson<{
        final_status?: string;
        error_message?: string;
        task_description?: string;
        task_config?: {
            resolved?: {
                description?: string;
                initial_prompt?: string;
            };
        };
        success_criteria_results?: RawCriterionResult[];
        post_failure_criteria_results?: RawCriterionResult[];
        iterations?: TurnEntry[];
        environment_info?: RawRunJson["environment_info"];
    }>(path.join(contentDir, "task.json"));

    const criteria = parseCriterionResults(task?.success_criteria_results);
    const postFailureCriteria = parseCriterionResults(
        task?.post_failure_criteria_results,
    );

    const artifactRoot = path.join(contentDir, "artifacts");
    // relPath is stored relative to the run root so the /api/file route can
    // resolve it against the source's <cacheDir>/<runId> without needing to know the task
    // subdir. New layout yields `default/<task_id>/00/artifacts/...`; flat
    // layout yields `default/<task_id>/artifacts/...`. `resolveSafePath`
    // validates parts[0] and parts[1], so both shapes pass the check.
    const artifactPrefix = path.relative(
        path.join(dir, runId),
        artifactRoot,
    );
    const artifacts = sortArtifacts(
        await walkArtifacts(artifactRoot, artifactPrefix),
    );

    const componentShas = extractComponentShas(task?.environment_info);

    const flowDebug = parseFlowDebug(criteria);
    const toolCalls = parseToolCalls(task?.iterations ?? []);
    const messages = parseMessages(task?.iterations ?? []);
    const providerCalls = parseProviderCalls(task?.iterations ?? []);
    // The per-message stream is the authoritative cumulative bill and is
    // preferred whenever it carries token data. Iteration token_usage comes
    // from the SDK ResultMessage snapshot, which is NOT cumulative for
    // cache/input tokens — on a multi-call run it under-reports
    // cache_read_input_tokens by the re-read cascade (each later call re-reads
    // the growing transcript and is billed for it), often 2-3x low. Anthropic
    // bills cache reads per request, so the true total is the sum of each
    // call's usage. parseMessages already collapses per message_id, so this sum
    // doesn't double-count the CLI's repeated usage dicts. Fall back to the
    // iteration aggregate only for runs that never recorded per-message tokens.
    const tokens = selectTokenTotals(messages, task?.iterations ?? []);

    const subAgentUsageByToolId = aggregateSubAgentUsage(messages);

    const taskDescription =
        task?.task_config?.resolved?.initial_prompt ??
        task?.task_config?.resolved?.description ??
        task?.task_description ??
        null;

    // The trailing text-only assistant message lives on the last turn's
    // ResultMessage. Walk from the end so partial/crashed turns earlier
    // in the run don't shadow it.
    let finalAssistantText: string | null = null;
    for (let i = (task?.iterations?.length ?? 0) - 1; i >= 0; i--) {
        const r = task?.iterations?.[i]?.result_summary?.result;
        if (typeof r === "string" && r.length > 0) {
            finalAssistantText = r;
            break;
        }
    }

    return {
        ...row,
        // task.json is the richer source: it carries the trailing text
        // even on legacy run.json files that predate has_final_reply.
        // Overriding row.hasFinalReply here keeps detail TURNS, Turn
        // timeline header, and the reply row in lockstep.
        hasFinalReply: finalAssistantText != null,
        runId,
        componentShas,
        finalStatus: task?.final_status ?? null,
        errorMessage: task?.error_message ?? null,
        taskDescription,
        criteria,
        postFailureCriteria,
        artifacts,
        flowDebug,
        toolCalls,
        finalAssistantText,
        messages,
        tokens,
        subAgentUsageByToolId,
        providerCalls,
    };
}

// Pick the run's authoritative cumulative token totals. The per-message stream
// run-level token totals, picking the more complete of two sources:
//   - iteration token_usage: on current runs this is built from the SDK
//     ResultMessage `model_usage` (cumulative per-model billing, reconciles to
//     total_cost_usd, and INCLUDES sub-agent cache-creation/input). On legacy
//     runs it is the old ResultMessage `usage` snapshot, which under-reports the
//     cache-read cascade ~2-3x.
//   - per-message stream sum: deduped per message_id; captures the cache-read
//     cascade but UNDER-reports sub-agent tokens (those emissions aren't fully
//     bubbled into the stream).
// Neither dominates across both run vintages, so pick the larger total — the
// more complete figure. New runs → iteration (model_usage) wins; legacy runs →
// per-message wins over the stale snapshot.
export function selectTokenTotals(
    messages: MessageEvent[],
    turns: TurnEntry[],
): TokenTotals {
    const fromMessages = sumMessageTokens(messages);
    // Current runs carry a synthetic reconciliation entry per turn, so the
    // message stream sums EXACTLY to the authoritative total — the stream is
    // self-reconciling and authoritative; no separate aggregate is consulted.
    // ("agent tokens" — the competing iteration aggregate — is retired here.)
    if (messages.some((m) => m.role === "reconciliation")) {
        return fromMessages;
    }
    // Legacy / pre-reconciliation runs: the stream may under-report (sub-agent
    // tokens, fixed prompt overhead). Fall back to the more-complete of the two
    // sources, as before.
    const fromIterations = sumTokenTotals(turns);
    return fromIterations.total >= fromMessages.total ? fromIterations : fromMessages;
}

// Token totals from the iteration token_usage aggregate (model_usage-derived on
// current runs; the ResultMessage snapshot on legacy runs).
function sumTokenTotals(turns: TurnEntry[]): TokenTotals {
    let input = 0;
    let output = 0;
    let cacheCreation = 0;
    let cacheRead = 0;
    for (const t of turns) {
        const tu = t.token_usage;
        if (!tu) continue;
        // Disjoint uncached slice: prefer the explicit field on current runs,
        // fall back to legacy `input_tokens` (which meant uncached back then).
        input += tu.uncached_input_tokens ?? tu.input_tokens ?? 0;
        output += tu.output_tokens ?? 0;
        cacheCreation += tu.cache_creation_input_tokens ?? 0;
        cacheRead += tu.cache_read_input_tokens ?? 0;
    }
    return {
        input,
        output,
        cacheCreation,
        cacheRead,
        total: input + output + cacheCreation + cacheRead,
    };
}

// Token totals from the collapsed per-message stream. MessageEvent tokens are
// already summed per message_id group (see parseMessages), so they don't
// double-count the CLI's repeated usage dicts. Captures the per-request
// cache-read cascade but under-reports sub-agent tokens (their emissions aren't
// fully bubbled up) — one of the two inputs selectTokenTotals chooses between.
function sumMessageTokens(messages: MessageEvent[]): TokenTotals {
    let input = 0;
    let output = 0;
    let cacheCreation = 0;
    let cacheRead = 0;
    for (const m of messages) {
        input += m.inputTokens ?? 0;
        output += m.outputTokens ?? 0;
        cacheCreation += m.cacheWriteTokens ?? 0;
        cacheRead += m.cacheReadTokens ?? 0;
    }
    return {
        input,
        output,
        cacheCreation,
        cacheRead,
        total: input + output + cacheCreation + cacheRead,
    };
}

export async function readRunAnalysis(
    runId: string,
    source: Source = DEFAULT_SOURCE,
): Promise<string | null> {
    const dir = runsDirFor(RUNS_DIR, source);
    await ensureRunAnalysis(source.container, runId, dir);
    return fs
        .readFile(path.join(dir, runId, "analysis.md"), "utf-8")
        .catch(() => null);
}

// ---------- run metadata (meta.json sidecar) ----------

// Optional, user-supplied at upload time. Null on pipeline runs and any run
// uploaded before the feature — every consumer must treat null as "no
// metadata" (title falls back to the run id, adhoc defaults false).
export interface RunMeta {
    title: string | null;
    description: string | null;
    adhoc: boolean;
}

interface RawRunMeta {
    title?: string | null;
    description?: string | null;
    adhoc?: boolean;
}

export async function readRunMeta(
    runId: string,
    source: Source = DEFAULT_SOURCE,
): Promise<RunMeta | null> {
    const dir = runsDirFor(RUNS_DIR, source);
    await ensureRunMeta(source.container, runId, dir);
    const raw = await readJson<RawRunMeta>(path.join(dir, runId, "meta.json"));
    if (!raw) return null;
    return {
        title: raw.title ?? null,
        description: raw.description ?? null,
        adhoc: raw.adhoc === true,
    };
}

export async function readLogTail(
    runId: string,
    taskId: string,
    replicate = 0,
    maxBytes = 200_000,
    source: Source = DEFAULT_SOURCE,
    variantId: string = DEFAULT_VARIANT_ID,
): Promise<string> {
    await ensureTaskDir(
        source.container,
        runId,
        taskId,
        runsDirFor(RUNS_DIR, source),
        variantId,
    );
    const taskDir = taskContentBase(runId, taskId, variantId, source);
    const contentDir = await resolveTaskContentDir(taskDir, replicate);
    const logPath = path.join(contentDir, "task.log");
    const raw = await fs.readFile(logPath, "utf-8").catch(() => "");
    if (raw.length <= maxBytes) return raw;
    return `… (truncated, showing last ${maxBytes} bytes)\n\n${raw.slice(-maxBytes)}`;
}

// One user↔agent exchange parsed from conversation.log (simulation runs only).
export interface ConversationTurn {
    role: "USER" | "AGENT";
    turn: number;
    metadata: string | null;
    text: string;
}

// Reads the clean simulation transcript. Returns "" for non-simulation tasks
// (single-shot runs never write conversation.log) — mirrors readLogTail.
export async function readConversationLog(
    runId: string,
    taskId: string,
    replicate = 0,
    maxBytes = 200_000,
    source: Source = DEFAULT_SOURCE,
    variantId: string = DEFAULT_VARIANT_ID,
): Promise<string> {
    await ensureTaskDir(
        source.container,
        runId,
        taskId,
        runsDirFor(RUNS_DIR, source),
        variantId,
    );
    const taskDir = taskContentBase(runId, taskId, variantId, source);
    const contentDir = await resolveTaskContentDir(taskDir, replicate);
    const logPath = path.join(contentDir, "conversation.log");
    const raw = await fs.readFile(logPath, "utf-8").catch(() => "");
    if (raw.length <= maxBytes) return raw;
    return `… (truncated, showing last ${maxBytes} bytes)\n\n${raw.slice(-maxBytes)}`;
}

// Splits conversation.log into ordered turns. The header line is:
//   === ROLE (turn N)[ — metadata] ===
// Everything up to the next header (or EOF) is that turn's body.
export function parseConversation(raw: string): ConversationTurn[] {
    const header = /^=== (USER|AGENT) \(turn (\d+)\)(?: — (.*?))? ===$/;
    const turns: ConversationTurn[] = [];
    let current: ConversationTurn | null = null;
    const body: string[] = [];
    const flush = () => {
        if (current) current.text = body.join("\n").trim();
        body.length = 0;
    };
    for (const line of raw.split("\n")) {
        const m = header.exec(line);
        if (m) {
            flush();
            current = {
                role: m[1] as "USER" | "AGENT",
                turn: Number(m[2]),
                metadata: m[3] ?? null,
                text: "",
            };
            turns.push(current);
        } else if (current) {
            body.push(line);
        }
    }
    flush();
    return turns;
}

// Collect every file under a task's folder (`<variantId>/<taskId>/`) for the
// download-as-zip button on the task page. Reuses walkArtifacts so the same
// noise filter (`.venv`, `node_modules`, `*.pyc`, lockfiles, secrets) and
// symlink skip that drive the Artifacts list also shape the zip — plus
// task.json / task.log at the task root, which aren't excluded by any pattern.
// Returns null for an invalid id or a missing/empty task dir.
export async function collectTaskFiles(
    runId: string,
    taskId: string,
    source: Source = DEFAULT_SOURCE,
    variantId: string = DEFAULT_VARIANT_ID,
): Promise<{ relPath: string; abs: string }[] | null> {
    if (!isValidId(runId) || !isValidTaskId(taskId)) return null;
    if (!isValidVariantId(variantId)) return null;
    await ensureTaskDir(
        source.container,
        runId,
        taskId,
        runsDirFor(RUNS_DIR, source),
        variantId,
    );
    const taskDir = taskContentBase(runId, taskId, variantId, source);
    const refs = await walkArtifacts(taskDir);
    if (refs.length === 0) return null;
    return refs.map((r) => ({ relPath: r.relPath, abs: path.join(taskDir, r.relPath) }));
}

// Collect every file under a whole run (`<runId>/`) for the download-as-zip
// button on the run page. Same noise filter / symlink skip as collectTaskFiles,
// applied across all task subdirs plus run-level files (run.json, analysis.md,
// meta.json, …). Returns null for an invalid id or a missing/empty run dir.
export async function collectRunFiles(
    runId: string,
    source: Source = DEFAULT_SOURCE,
): Promise<{ relPath: string; abs: string }[] | null> {
    if (!isValidId(runId)) return null;
    const dir = runsDirFor(RUNS_DIR, source);
    await ensureRunDir(source.container, runId, dir);
    const runDir = path.join(dir, runId);
    const refs = await walkArtifacts(runDir);
    if (refs.length === 0) return null;
    return refs.map((r) => ({ relPath: r.relPath, abs: path.join(runDir, r.relPath) }));
}

export async function resolveSafePath(
    runId: string,
    relPath: string,
    source: Source = DEFAULT_SOURCE,
): Promise<string | null> {
    if (!isValidId(runId)) return null;
    const dir = runsDirFor(RUNS_DIR, source);
    // Artifact URLs embed the variant + task subdirs in relPath
    // (`<variant-id>/<task-id>/artifacts/...`) — extract them so the narrow
    // fetch hits the right blobs without pulling the whole run. `activation` is
    // excluded because its prefix nests one level deeper
    // (`activation/default/<task-id>/…`), so parts[0..1] are not (variant, task)
    // there; that case falls through to the run-summary fetch as it always did.
    const parts = relPath.split("/");
    if (parts[0] !== "activation" && isValidVariantId(parts[0]) && parts[1]) {
        if (!isValidId(parts[1])) return null;
        await ensureTaskDir(source.container, runId, parts[1], dir, parts[0]);
    } else {
        await ensureRunSummary(source.container, runId, dir);
    }
    const base = path.join(dir, runId);
    const baseReal = await fs.realpath(base).catch(() => null);
    if (!baseReal) return null;
    const candidate = path.resolve(baseReal, relPath);
    if (
        candidate !== baseReal &&
        !candidate.startsWith(baseReal + path.sep)
    ) {
        return null;
    }
    // Canonicalize the target to catch symlinks under the run dir that point
    // outside it. If the target doesn't exist yet, fall back to the lexical
    // candidate so the caller can distinguish "outside dir" from "not found".
    const candidateReal = await fs.realpath(candidate).catch(() => null);
    if (candidateReal == null) return candidate;
    if (
        candidateReal !== baseReal &&
        !candidateReal.startsWith(baseReal + path.sep)
    ) {
        return null;
    }
    return candidateReal;
}

// Delete a run's locally-cached blob copy under `root` so the next view
// re-downloads it from storage. `force: true` makes a never-cached run a
// harmless no-op. Returns false (deleting nothing) for an unsafe id — note
// isValidId still admits "." and ".." (dots are word-ish), so require the
// resolved target to be a strict child of `root` before rm can run, or a "."
// id would nuke the cache root and ".." its parent.
export async function clearRunCacheDir(
    root: string,
    id: string,
): Promise<boolean> {
    if (!isValidId(id)) return false;
    const base = path.resolve(root);
    const dir = path.resolve(base, id);
    if (dir === base || !dir.startsWith(base + path.sep)) return false;
    await fs.rm(dir, { recursive: true, force: true });
    return true;
}
