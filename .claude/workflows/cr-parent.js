export const meta = {
  name: 'coder-eval-code-review-wf',
  description: 'Workflow 8-axis coder_eval review: per-axis sub-workflows, adversarial verify, deterministic synthesis + rendering',
  phases: [
    { title: 'Review & Verify', detail: 'one sub-workflow per axis; each verifies its own medium+ findings' },
    { title: 'Synthesize', detail: "what's-missing + harness/lint + verdict; then render the report" },
  ],
}

// --- scoring weights: keep in sync with the sibling command's Scoring section ---
const WEIGHT = { critical: 3.0, high: 1.0, medium: 0.5, low: 0.1 }
const round1 = (x) => Math.round(x * 10) / 10
const EMOJI = { critical: '🔴', high: '🟠', medium: '🟡', low: '🔵' }
const SEVNAME = { critical: 'Critical', high: 'High', medium: 'Medium', low: 'Low' }
const SEV_RANK = { critical: 0, high: 1, medium: 2, low: 3 }
const AXIS_FILE = {
  1: '01-code-quality', 2: '02-type-safety', 3: '03-test-health', 4: '04-security',
  5: '05-architecture', 6: '06-error-handling', 7: '07-api-surface', 8: '08-harness-quality',
}

const MISSING_SCHEMA = {
  type: 'object',
  required: ['items'],
  properties: {
    items: {
      type: 'array',
      items: {
        type: 'object',
        required: ['bucket', 'severity', 'text'],
        properties: {
          bucket: { type: 'string', enum: ['parallel-paths', 'tests', 'downstream-consumers', 'display-mapping', 'daily-nightly'] },
          severity: { type: 'string', enum: ['critical', 'high', 'medium', 'low'] },
          text: { type: 'string' },
          trigger_file: { type: 'string' },
          restates_finding: { type: 'string' },
        },
        additionalProperties: false,
      },
    },
  },
  additionalProperties: false,
}
const HARNESS_SCHEMA = {
  type: 'object',
  required: ['static_checks', 'harness_improvements'],
  properties: {
    static_checks: {
      type: 'array',
      items: {
        type: 'object',
        required: ['proposal', 'prevents'],
        properties: {
          proposal: { type: 'string' },
          kind: { type: 'string', enum: ['ce-lint', 'ruff', 'pyright', 'bandit-codeql'] },
          prevents: { type: 'string' },
        },
        additionalProperties: false,
      },
    },
    harness_improvements: {
      type: 'array',
      items: {
        type: 'object',
        required: ['proposal', 'why_not_static'],
        properties: {
          proposal: { type: 'string' },
          why_not_static: { type: 'string' },
          prevents: { type: 'string' },
        },
        additionalProperties: false,
      },
    },
  },
  additionalProperties: false,
}
const SUMMARY_SCHEMA = {
  type: 'object',
  required: ['verdict', 'top5'],
  properties: {
    verdict: { type: 'string' },
    top5: { type: 'array', items: { type: 'string' } },
  },
  additionalProperties: false,
}
const MERGE_SCHEMA = {
  type: 'object',
  required: ['groups'],
  properties: {
    groups: {
      type: 'array',
      items: {
        type: 'object',
        required: ['keep', 'merge', 'reason'],
        properties: {
          keep: { type: 'integer' },
          merge: { type: 'array', items: { type: 'integer' } },
          canonical_severity: { type: 'string', enum: ['critical', 'high', 'medium', 'low'] },
          canonical_title: { type: 'string' },
          reason: { type: 'string' },
        },
        additionalProperties: false,
      },
    },
  },
  additionalProperties: false,
}

// `args` may arrive as a JSON string (tool serialization) — normalize to an object.
const ARGS = typeof args === 'string' ? JSON.parse(args) : args
const META = ARGS.meta ?? {}

// ---- Phase 1: one sub-workflow per axis, in parallel ----
// `cr-axis` is the sibling named workflow in .claude/workflows/ (review + adversarial
// verify/correct of that axis's medium+ findings); referenced by name, no path plumbing.
phase('Review & Verify')
const axisResults = (
  await parallel(ARGS.axes.map((ax) => () => workflow('cr-axis', { axis: ax, shared: ARGS.shared })))
).filter(Boolean)

// ---- verification ledger (false-positive accounting from the verify stage) ----
const verification = {
  perAxis: axisResults.map((r) => ({
    axis: r.axis,
    name: r.name,
    ...(r.verifyStats || { proposed: 0, verified: 0, refuted: 0, corrected: 0, lows: 0 }),
  })),
  refuted: axisResults.flatMap((r) => r.refuted || []),
}
verification.totals = verification.perAxis.reduce(
  (t, x) => ({
    proposed: t.proposed + x.proposed,
    verified: t.verified + x.verified,
    refuted: t.refuted + x.refuted,
    corrected: t.corrected + x.corrected,
    lows: t.lows + x.lows,
  }),
  { proposed: 0, verified: 0, refuted: 0, corrected: 0, lows: 0 },
)

// ---- flatten raw findings (stable index for the dedup pass) ----
const rawFindings = []
for (const r of axisResults) {
  for (const f of r.findings ?? []) {
    rawFindings.push({
      axisNum: r.axis,
      axisName: r.name,
      severity: f.severity,
      title: f.title,
      file: f.file,
      line: f.line ?? '',
      recommendation: f.recommendation,
      cvss: f.cvss ?? '',
      verdict_note: f.verdict_note ?? '',
    })
  }
}

// ---- Phase 2: dedup + theme-group BEFORE scoring (one semantic agent) ----
phase('Synthesize')
const sib = ARGS.shared.siblingPath
const indexed = rawFindings
  .map((f, i) => `${i}: [A${f.axisNum} ${f.severity}] ${f.title} @ ${f.file}:${f.line}`)
  .join('\n')
const mergePlan = await agent(
`You are de-duplicating and theme-grouping confirmed code-review findings BEFORE they are scored, so the same issue is not counted multiple times and a single theme is not scored N times (which would unfairly tank an axis). Each finding has an integer index.

Return "groups". Each group merges 2+ findings that are EITHER (a) the SAME root cause surfaced under different axes or at the same location, OR (b) the SAME mechanical theme/class (e.g. several god-functions / high-CC functions; several instances of one stringly-typed-dict pattern). For each group set:
- keep: index of the single canonical finding to retain (prefer the highest-severity, most-precise one),
- merge: the other indices folded into it (do NOT include keep),
- canonical_severity (optional): severity the merged finding scores at (default = keep's severity),
- canonical_title (optional): a title covering the whole cluster,
- reason: one line.

Be CONSERVATIVE: only group findings that are genuinely the same root cause or the same mechanical theme. Distinct bugs — even in the same file — stay separate. Any index you do not place in a group is kept and scored as-is.

## Findings
${indexed}`,
  { label: 'dedup-group', phase: 'Synthesize', schema: MERGE_SCHEMA },
)

const mergedAway = new Set()
const groupByKeep = new Map()
for (const g of (mergePlan && mergePlan.groups) || []) {
  if (typeof g.keep !== 'number') continue
  groupByKeep.set(g.keep, g)
  for (const m of g.merge || []) if (m !== g.keep) mergedAway.add(m)
}
const all = []
rawFindings.forEach((f, idx) => {
  if (mergedAway.has(idx)) return
  const g = groupByKeep.get(idx)
  if (g) {
    if (g.canonical_severity) f.severity = g.canonical_severity
    if (g.canonical_title) f.title = g.canonical_title
    const members = (g.merge || []).filter((m) => m !== idx).map((m) => rawFindings[m]).filter(Boolean)
    if (members.length) {
      f.grouped = members.map((m) => `${m.file}:${m.line} [A${m.axisNum} ${m.severity}]`)
      f.groupNote = g.reason || ''
    }
  }
  all.push(f)
})

// ---- deterministic per-axis scoring from the DEDUPED set (exact; no agent arithmetic) ----
const scored = axisResults.map((r) => {
  const counts = { critical: 0, high: 0, medium: 0, low: 0 }
  for (const f of all) {
    if (f.axisNum === r.axis && counts[f.severity] !== undefined) counts[f.severity] += 1
  }
  const rawScore =
    10 - WEIGHT.critical * counts.critical - WEIGHT.high * counts.high - WEIGHT.medium * counts.medium - WEIGHT.low * counts.low
  return { axis: r.axis, name: r.name, counts, score: Math.max(0, round1(rawScore)) }
})
const overall = scored.length ? round1(scored.reduce((sum, x) => sum + x.score, 0) / scored.length) : 0
const weakest = scored.reduce((m, x) => (x.score < m.score ? x : m), scored[0] ?? { name: '-', score: 0 })

// ---- annotate cross-axis convergence on the deduped set (non-destructive) ----
const axesAtLoc = new Map()
for (const f of all) {
  const key = `${f.file}:${f.line}`
  if (!axesAtLoc.has(key)) axesAtLoc.set(key, new Set())
  axesAtLoc.get(key).add(f.axisNum)
}
for (const f of all) {
  const axesHere = [...axesAtLoc.get(`${f.file}:${f.line}`)].sort((x, y) => x - y)
  f.crossAxis = axesHere.length > 1 ? axesHere : null
}

// ---- synthesis agents (read their own instructions from the sibling) ----
const findingsBlob = JSON.stringify(all, null, 1)

const [whatsMissing, harnessLint, summary] = await parallel([
  () => agent(
`Read the "Derive the \\"What's Missing\\" list" bullet under step 5 of "## Procedure" in ${sib} — that is your instruction set (the five buckets and the severity rules). Then, given the in-scope files and the confirmed findings below, produce the What's Missing items.

If an item restates or shares a root cause with an existing finding, set restates_finding to "Axis <N>: <short title>" so the report links them instead of double-reporting.

## In-scope files
${ARGS.shared.fileList}

## Confirmed findings (JSON)
${findingsBlob}`,
    { label: 'whats-missing', phase: 'Synthesize', schema: MISSING_SCHEMA },
  ),
  () => agent(
`Read the "Derive \\"Harness & Lint Improvements\\"" bullet under step 5 of "## Procedure" in ${sib} — that is your instruction set (static-check-first; the two buckets). Then, given the confirmed findings below, produce the harness/lint improvements.

## Confirmed findings (JSON)
${findingsBlob}`,
    { label: 'harness-lint', phase: 'Synthesize', schema: HARNESS_SCHEMA },
  ),
  () => agent(
`You are writing the executive summary for a code review of coder_eval (overall score ${overall}/10, weakest axis ${weakest.name} at ${weakest.score}/10). Given the per-axis scores and confirmed findings below, return:
- verdict: ONE sentence — what's healthy, what the real risks are, and the bottom line.
- top5: the 5 highest-leverage priority actions, each one sentence with the file:line where relevant, ordered by impact. Put anything that can change a task's score/final_status for identical agent output first.

## Per-axis scores (JSON)
${JSON.stringify(scored)}

## Confirmed findings (JSON)
${findingsBlob}`,
    { label: 'verdict-top5', phase: 'Synthesize', schema: SUMMARY_SCHEMA },
  ),
])

const requestedAxisNums = ARGS.axes.map((ax) => ax.num)
const presentAxisNums = scored.map((s) => s.axis)
const missingAxes = requestedAxisNums.filter((n) => !presentAxisNums.includes(n))

// ---- deterministic markdown rendering (returns ready-to-write files) ----
const totals = all.reduce((t, f) => { t[f.severity] += 1; return t }, { critical: 0, high: 0, medium: 0, low: 0 })

const findingsFor = (axisNum) =>
  all.filter((f) => f.axisNum === axisNum).sort((x, y) => SEV_RANK[x.severity] - SEV_RANK[y.severity])
const topIssue = (axisNum) => { const fs = findingsFor(axisNum); return fs.length ? fs[0].title : '—' }

let summaryTable = '| Axis | Score | 🔴 | 🟠 | 🟡 | 🔵 | Top Issue |\n|------|-------|----|----|----|----|-----------|\n'
for (const s of scored) {
  summaryTable += `| ${s.axis}. ${s.name} | ${s.score} / 10 | ${s.counts.critical} | ${s.counts.high} | ${s.counts.medium} | ${s.counts.low} | ${topIssue(s.axis)} |\n`
}

const chFindings = all
  .filter((f) => f.severity === 'critical' || f.severity === 'high')
  .sort((x, y) => SEV_RANK[x.severity] - SEV_RANK[y.severity] || x.axisNum - y.axisNum)
const chBlock = chFindings.length
  ? chFindings.map((f, i) => {
      let s = `${i + 1}. **[Axis ${f.axisNum}] ${f.title}** — \`${f.file}:${f.line}\`. ${f.recommendation}`
      if (f.cvss) s += ` _${f.cvss}_`
      if (f.crossAxis) s += ` (Cross-axis: flagged by axes ${f.crossAxis.join(', ')})`
      if (f.grouped) s += ` [groups ${f.grouped.length}: ${f.grouped.join('; ')}]`
      return s
    }).join('\n')
  : 'No 🔴 / 🟠 findings.'

const BUCKET_LABEL = {
  'parallel-paths': 'Parallel paths', 'tests': 'Tests', 'downstream-consumers': 'Downstream consumers',
  'display-mapping': 'Display & mapping dicts', 'daily-nightly': 'Daily/nightly',
}
const missingBlock = (() => {
  const items = (whatsMissing && whatsMissing.items) || []
  if (!items.length) return 'Nothing identified.'
  const byBucket = {}
  for (const it of items) { (byBucket[it.bucket] = byBucket[it.bucket] || []).push(it) }
  let out = ''
  for (const b of Object.keys(byBucket)) {
    out += `**${BUCKET_LABEL[b] || b}:**\n`
    for (const it of byBucket[b]) {
      let line = `- ${EMOJI[it.severity] || ''} ${it.text}`
      if (it.trigger_file) line += ` _(trigger: ${it.trigger_file})_`
      if (it.restates_finding) line += ` _(restates: ${it.restates_finding})_`
      out += line + '\n'
    }
    out += '\n'
  }
  return out.trim()
})()

const harnessBlock = (() => {
  const sc = (harnessLint && harnessLint.static_checks) || []
  const hi = (harnessLint && harnessLint.harness_improvements) || []
  if (!sc.length && !hi.length) return 'Nothing identified.'
  let out = '**Static checks (lint / type):**\n'
  out += sc.length ? sc.map((x) => `- ${x.kind ? '[' + x.kind + '] ' : ''}${x.proposal} _Prevents:_ ${x.prevents}`).join('\n') : '- (none)'
  out += '\n\n**Harness improvements (not statically reachable):**\n'
  out += hi.length ? hi.map((x) => `- ${x.proposal} _Why not static:_ ${x.why_not_static || ''} _Prevents:_ ${x.prevents || ''}`).join('\n') : '- (none)'
  return out
})()

const top5 = (summary && summary.top5) || []
const top5Block = top5.length ? top5.map((t, i) => `${i + 1}. ${t}`).join('\n') : '(none)'
const verdict = (summary && summary.verdict) || ''

const vt = verification.totals
const refutalRate = vt.verified ? Math.round((vt.refuted / vt.verified) * 100) : 0
const refutedBlock = verification.refuted.length
  ? verification.refuted.map((f, i) => `${i + 1}. [Axis ${f.axisNum} ${f.severity}] ${f.title} — \`${f.file}:${f.line}\` — _refuted:_ ${f.reason || '(no reason given)'}`).join('\n')
  : 'None — every medium+ finding was confirmed (no false positives this run).'
const verificationBlock =
`**Medium+ findings:** ${vt.verified} verified · **${vt.refuted} refuted as false-positive (dropped)** · ${vt.corrected} corrected in place · ${vt.lows} low passed through unverified. Refutal rate: ${refutalRate}%.

_Refuted (dropped) findings — compare run-over-run to confirm the same false positives keep being caught:_

${refutedBlock}`

const metaBlock =
`## Review Metadata
- Timestamp: ${META.timestamp || '?'}
- Git SHA: ${META.sha || '?'}
- Branch: ${META.branch || '?'}
- Scope: ${META.scope || ARGS.shared.scopeSpec}
- Axes reviewed: ${scored.map((s) => s.axis).join(',')}
- Model: ${META.model || '?'}
- Orchestration: workflow variant (per-axis sub-workflows, adversarial verify/correct, deterministic JS scoring + rendering)
- Verify stage: ${ARGS.shared.verify ? 'ON' : 'OFF'}${missingAxes.length ? `\n- Missing axes (no result): ${missingAxes.join(', ')}` : ''}`

const summaryMd =
`${metaBlock}

## Summary

${summaryTable}
**Overall Score**: ${overall} / 10 (mean of reviewed axes)
**Weakest Axis**: ${weakest.name} at ${weakest.score} / 10
**Totals**: 🔴 ${totals.critical} · 🟠 ${totals.high} · 🟡 ${totals.medium} · 🔵 ${totals.low} across ${scored.length} axes.

## Verdict

${verdict}

## Critical & High Issues (🔴 / 🟠)

${chBlock}

## What's Missing

${missingBlock}

## Harness & Lint Improvements

${harnessBlock}

## Top 5 Priority Actions

${top5Block}

## Verification (false-positive ledger)

${verificationBlock}
`

const axisMd = (s) => {
  const ax = ARGS.axes.find((x) => x.num === s.axis) || {}
  const v = verification.perAxis.find((p) => p.axis === s.axis) || { verified: 0, refuted: 0, corrected: 0 }
  const fs = findingsFor(s.axis)
  let out =
`### Axis ${s.axis}: ${s.name}
**Score**: ${s.score} / 10
**Counts**: 🔴 ${s.counts.critical} · 🟠 ${s.counts.high} · 🟡 ${s.counts.medium} · 🔵 ${s.counts.low}
**Verification**: ${v.verified} verified · ${v.refuted} refuted (false-positive) · ${v.corrected} corrected

**Automated Results**: ${ax.automatedSummary || '—'}

**Findings**:
`
  if (!fs.length) return out + '_No findings._\n'
  fs.forEach((f, i) => {
    out += `${i + 1}. [${EMOJI[f.severity]} ${SEVNAME[f.severity]}] ${f.title}\n`
    out += `   - File(s): ${f.file}:${f.line}\n`
    out += `   - Recommendation: ${f.recommendation}\n`
    if (f.grouped) out += `   - Groups ${f.grouped.length} related: ${f.grouped.join('; ')}${f.groupNote ? ' — ' + f.groupNote : ''}\n`
    if (f.cvss) out += `   - CVSS vector: ${f.cvss}\n`
    if (f.crossAxis) out += `   - Cross-axis: flagged by axes ${f.crossAxis.join(', ')}\n`
    if (f.verdict_note) out += `   - Verified: ${f.verdict_note}\n`
  })
  return out
}

const prList = (arr) =>
  arr.length
    ? arr.sort((x, y) => x.axisNum - y.axisNum).map((f, i) => {
        let s = `${i + 1}. **[Axis ${f.axisNum}] ${f.title}** (\`${f.file}:${f.line}\`) — ${f.recommendation}`
        if (f.cvss) s += ` ${f.cvss}`
        return s
      }).join('\n')
    : 'None.'
const blockers = all.filter((f) => f.severity === 'critical' || f.severity === 'high')
const nonBlocking = all.filter((f) => f.severity === 'medium')
const nits = all.filter((f) => f.severity === 'low')

const prMd =
`# Review: coder_eval — ${META.scope || ARGS.shared.scopeSpec}

Scope: ${META.scope || ARGS.shared.scopeSpec} · branch \`${META.branch || '?'}\` · \`${(META.sha || '').slice(0, 7)}\` · ${META.timestamp || '?'} · workflow variant

${verdict}

## Summary

${summaryTable}
**Overall Score**: ${overall} / 10 · **Weakest Axis**: ${weakest.name} at ${weakest.score} / 10
**Totals**: 🔴 ${totals.critical} · 🟠 ${totals.high} · 🟡 ${totals.medium} · 🔵 ${totals.low} across ${scored.length} axes.

## Blockers

${prList(blockers)}

## Non-blocking, but please consider before merge

${prList(nonBlocking)}

## Nits

${prList(nits)}

## What's Missing

${missingBlock}

## Harness & Lint Improvements

${harnessBlock}

## Top 5 Priority Actions

${top5Block}

---

**Stats:** ${totals.critical} 🔴 · ${totals.high} 🟠 · ${totals.medium} 🟡 · ${totals.low} 🔵 across ${scored.length} axes reviewed.
`

const files = { '00-summary.md': summaryMd, '99-pr-comment.md': prMd }
for (const s of scored) files[`${AXIS_FILE[s.axis]}.md`] = axisMd(s)
files['results.json'] = JSON.stringify(
  { meta: META, scored, overall, weakest: { name: weakest.name, score: weakest.score }, totals, verification, findings: all, whatsMissing, harnessLint, summary, missingAxes },
  null,
  2,
)

return {
  scored,
  overall,
  weakest: { name: weakest.name, score: weakest.score },
  findings: all,
  whatsMissing,
  harnessLint,
  summary,
  verification,
  missingAxes,
  files,
}
