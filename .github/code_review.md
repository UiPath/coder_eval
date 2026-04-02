# Code Review Guidelines

Review this PR focusing on:
- **Architecture**: Does new code live in the right layer? Flag leaky abstractions and reversed dependencies
- **Correctness**: Logic errors, edge cases, off-by-one, truthiness traps, None misuse
- **Security**: Injection, credential exposure, unsafe inputs, ReDoS
- **Performance**: Unnecessary allocations, blocking I/O in async, missing pagination
- **Maintainability**: Unclear naming, missing error handling, code duplication
- **Config & data files**: Copy-paste errors in YAMLs, stale docs after behavior changes
- **Test coverage**: New logic without tests, bug fixes without regression tests
- **DRY**: Duplicated logic to consolidate — but also premature abstraction
- **Simplicity**: No unnecessary abstractions or speculative features
- **Alignment**: Adherence to CLAUDE.md guidelines and project conventions

Be concise. Only flag issues that matter. Skip style nits.

## Severity Levels

- **Critical**: System broken, data loss, security vulnerability — must fix before merge
- **High**: Bugs, broken contracts, stale references to removed features — must fix before merge
- **Medium**: Missing tests, DRY violations, poor naming, inconsistency with existing patterns — should fix
- **Low**: Minor readability improvements, suggestions for future refactoring — optional

## Review Process

- Read full files, not just diffs. For removal PRs, search the repo for stale references. For renames, grep for the old name.
- Read existing PR comments and review threads to avoid repeating resolved issues or contradicting prior discussion.

### Cross-file consistency checks

When a field, type, or enum is added or changed:
1. **Grep the full codebase** (`src/` and `tests/`) for all usages of the old pattern. Flag any that weren't updated.
2. **Compare parallel models**: If the same field exists on multiple models (e.g., `RunSummary` and `VariantAggregate`), verify the type, default, and validation are consistent. Flag mismatches.
3. **Check exhaustiveness**: If an enum, Literal, or status set changed, verify all `dict` lookups, `if/match` chains, and display mappings handle every value. Flag any that fall through to a generic default like `"?"`.

### "What's missing" analysis

After reviewing what changed, explicitly ask: **what should have changed but didn't?**
- **Parallel code paths**: If `batch.py` was updated, was `experiment.py` also updated (and vice versa)?
- **Tests**: Is there a test for every new code path, display element, and edge case? If a report row was added, is there a test with non-zero values?
- **Downstream consumers**: If a counting/classification formula changed, were all places that compute rates, averages, or percentages from those counts also updated?
- **Display/icon/mapping dicts**: If new enum values were added, do all rendering dicts cover them?

### Design-level scrutiny

Don't just verify the code is correct — ask whether the approach is robust:
- **Denylist vs allowlist**: If code uses `not in (A, B)` instead of `in (C, D, E)`, flag it. Denylists silently absorb new values; allowlists force explicit classification.
- **Implicit defaults**: If a field defaults to `0` or `None`, ask whether that silently swallows missing data vs. failing loudly.
- **Invariants**: If a set of fields should always sum to a total, ask whether that invariant is enforced (validator) or just happens to hold.

## Output Format

Post review as a SINGLE PR comment (not inline comments) using this structure:

```markdown
## Summary

<What this PR does and why>

## Change-by-Change Review

#### 1. <file or logical change>
<Severity: Critical / High / Medium / Low / OK>
<What changed, whether it's correct, and any issues>

#### 2. <file or logical change>
...

## What's Missing

<List anything that should have been changed/added but wasn't. If nothing, write "Nothing identified.">
- Missing tests for X
- Y dict/mapping not updated for new enum value Z
- Parallel code path in A.py not updated to match B.py

## Area Ratings

| Area | Status | Notes |
|------|--------|-------|
| Architecture | OK / Issue | — |
| Correctness | OK / Issue | — |
| Security | OK / Issue | — |
| Performance | OK / Issue | — |
| Maintainability | OK / Issue | — |
| Config & data files | OK / Issue | — |
| Test coverage | OK / Issue | — |
| DRY | OK / Issue | — |
| Simplicity | OK / Issue | — |
| Alignment | OK / Issue | — |

## Issues for Manual Review

<Bulleted list of anything the reviewer should verify that the automated review cannot — e.g., behavioral correctness, product intent, edge cases needing domain knowledge. If none, write "None found.">

## Conclusion

<Overall assessment — approve, request changes, or note concerns>
```

- Only report real issues. Each must reference the file path and line number.
- Only elaborate on issues rated Medium or above — mark clean changes as OK and move on.
- If the PR is clean: `## Summary\n\n<what the PR does>\n\n## Conclusion\n\nNo issues found.`
