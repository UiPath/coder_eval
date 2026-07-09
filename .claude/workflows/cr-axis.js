export const meta = {
  name: 'cr-axis',
  description: 'Review one coder_eval quality axis; adversarially verify each medium+ finding, correcting or dropping it',
  phases: [
    { title: 'Review' },
    { title: 'Verify' },
  ],
}

const FINDING_SCHEMA = {
  type: 'object',
  required: ['severity', 'title', 'file', 'recommendation'],
  properties: {
    severity: { type: 'string', enum: ['critical', 'high', 'medium', 'low'] },
    title: { type: 'string' },
    file: { type: 'string' },
    line: { type: 'string' },
    recommendation: { type: 'string' },
    cvss: { type: 'string' },
  },
  additionalProperties: false,
}
const AXIS_SCHEMA = {
  type: 'object',
  required: ['findings'],
  properties: {
    findings: { type: 'array', items: FINDING_SCHEMA },
    automated_disposition: { type: 'string' },
  },
  additionalProperties: false,
}
const VERDICT_SCHEMA = {
  type: 'object',
  required: ['refuted'],
  properties: {
    refuted: { type: 'boolean' },
    note: { type: 'string' },
    corrected_title: { type: 'string' },
    corrected_detail: { type: 'string' },
  },
  additionalProperties: false,
}

// `args` may arrive as a JSON string (tool serialization) — normalize to an object.
const ARGS = typeof args === 'string' ? JSON.parse(args) : args
const a = ARGS.axis
const s = ARGS.shared
const VERIFY_AT_OR_ABOVE = new Set(['medium', 'high', 'critical'])

phase('Review')
const reviewPrompt =
`You are reviewing **Axis ${a.num}: ${a.name}** of the coder_eval codebase.

## Rubric — READ THIS FIRST (do not work from memory)
Open .claude/shared/review-rubric.md and read its "## Review Principles" section — these are your binding principles.
Then open ${s.siblingPath} and read these sections; they are your binding rubric:
- "## Severity Standard" — the entire section, including the per-axis anchor table, the "Axis 4 (Security) additional requirement", and the "Axis 8 (Harness) scoring-correctness rule".
- "## Output Format" — the per-axis finding shape.
- The "Techniques to apply" fenced block (inside step 4 of "## Procedure") — run every check it lists while reading the code (including the extension-point conformance checks).
Calibrate severity against that anchor table exactly; do not summarize it from memory.

## Output
Return findings as structured data ONLY — do NOT compute a score (it is derived from your severity tags downstream). Put the line number (or "n/a") in \`line\` as a string.${a.num === 4 ? ' For every finding include a CVSS v3.1 vector in the `cvss` field; its base score must match the severity column.' : ''}

**Accuracy gate (before filing each finding):** every \`file\`, \`line\`, and metric you cite MUST come from a direct Read of that exact location (or verbatim from the routed tool output) — never an estimate. Quote the specific offending line(s) inside your \`recommendation\` so the fact is self-evidencing. Wrong line numbers and metrics are the single thing the downstream verifier most often has to correct; getting them exact here is cheaper than a correction round.

**Automated-signal disposition:** in \`automated_disposition\`, account for EACH distinct signal in "Automated results routed to this axis" below — for each, state whether you filed it (as which finding #) or did NOT file it and why (e.g. "tracked debt — out of scope", "false positive on re-read", "below this axis's bar"). This makes a genuinely clean axis read as deliberately clean rather than as if the tool output was ignored. If no automated signal was routed, write "none routed".

## Scope
${s.scopeSpec}
You may read ANY file in the repo for context, but every finding's \`file\` MUST be one of these in-scope files:
${s.fileList}
${s.scopeRules}
${s.prContentRule}

## Packages (authoritative)
${s.packages}

## Automated results routed to this axis
${a.automatedSummary}

## Your starting point (entry points, not an exhaustive list)
${a.startingPoint}`

const review = await agent(reviewPrompt, {
  label: `review:axis${a.num}`,
  phase: 'Review',
  schema: AXIS_SCHEMA,
})
// Review agent died/skipped (terminal API error or user skip). Signal it so the parent
// routes this axis into `missingAxes` instead of scoring an empty finding set as a clean 10/10.
if (!review) return { axis: a.num, name: a.name, reviewFailed: true, findings: [], automatedDisposition: '' }

const raw = review.findings ?? []

// Low findings pass through unverified. Medium+ get adversarially verified
// (unless --no-verify): refuted → dropped; real-but-inaccurate → corrected.
if (!s.verify) {
  return {
    axis: a.num,
    name: a.name,
    findings: raw.map((f) => ({ ...f, kept: true })),
    refuted: [],
    automatedDisposition: review.automated_disposition ?? '',
    verifyStats: { proposed: raw.length, verified: 0, refuted: 0, corrected: 0, lows: raw.length },
  }
}

const lows = raw.filter((f) => !VERIFY_AT_OR_ABOVE.has(f.severity)).map((f) => ({ ...f, kept: true }))
const toVerify = raw.filter((f) => VERIFY_AT_OR_ABOVE.has(f.severity))

phase('Verify')
const verified = await parallel(
  toVerify.map((f) => () =>
    agent(
`Adversarially verify this code-review finding against the coder_eval codebase. Your job is to REFUTE it if you honestly can.

Axis ${a.num} (${a.name}) finding: ${f.title}
Location: ${f.file}:${f.line ?? '?'}
Claimed problem / recommendation: ${f.recommendation}

${s.prContentRule}
In-scope files: ${s.fileList}

Read the ACTUAL code at that location and its surrounding context, then:
- Set refuted=true if the cited code doesn't exist or doesn't do what the finding claims, the concern is already handled/correct elsewhere, or it is speculation with no concrete, demonstrable defect. Default to refuted=true when you cannot concretely confirm a real problem.
- If the finding is REAL but a stated DETAIL is wrong (wrong line number, line count, complexity/metric value, or symbol name), set refuted=false and return a corrected_title (and/or a corrected_detail note) carrying the VERIFIED facts, so the report ships accurate numbers instead of the reviewer's estimate.
Return refuted, plus optional note / corrected_title / corrected_detail.`,
      { label: `verify:axis${a.num}`, phase: 'Verify', schema: VERDICT_SCHEMA },
    )
      .then((v) => {
        const refuted = v ? v.refuted === true : false
        const corrected = v ? Boolean(v.corrected_title || v.corrected_detail) : false
        return {
          ...f,
          kept: !refuted,
          verifyStatus: refuted ? 'refuted' : corrected ? 'corrected' : 'confirmed',
          title: v && v.corrected_title ? v.corrected_title : f.title,
          original_title: f.title,
          verdict_note: v ? [v.corrected_detail, v.note].filter(Boolean).join(' — ') : 'verifier unavailable — kept',
        }
      })
      .catch(() => ({ ...f, kept: true, verifyStatus: 'unverified', verdict_note: 'verifier errored — kept' })),
  ),
)

// Separate refuted (false-positive) findings from the confirmed/corrected ones.
const checked = verified.filter(Boolean)
const confirmed = checked.filter((f) => f.kept !== false)
const refuted = checked
  .filter((f) => f.kept === false)
  .map((f) => ({ axisNum: a.num, axisName: a.name, severity: f.severity, title: f.original_title || f.title, file: f.file, line: f.line ?? '', reason: f.verdict_note }))
return {
  axis: a.num,
  name: a.name,
  findings: [...confirmed, ...lows],
  refuted,
  automatedDisposition: review.automated_disposition ?? '',
  verifyStats: {
    proposed: raw.length,
    verified: toVerify.length,
    refuted: refuted.length,
    corrected: confirmed.filter((f) => f.verifyStatus === 'corrected').length,
    lows: lows.length,
  },
}
