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
- If the PR is clean: `## Summary\n\n<what the PR does>\n\n## Conclusion\n\nNo issues found. ✅`
