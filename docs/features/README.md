# Feature Documentation

This directory contains design documents and feature specs for the coder_eval framework.

## Naming Convention

All files **must** be prefixed with the creation date in `YYYY-MM-DD` format:

```
YYYY-MM-DD-<feature-name>.md
```

Examples:
- `2026-03-07-ast-similarity-design.md`
- `2026-03-02-timeout-feature.md`

This convention is enforced by a pre-commit hook. Commits adding files that don't
match the pattern will be rejected.

## Document Template

Feature docs should include a **Related PR** section linking to the associated pull request:

```markdown
# Feature Title

**Related PR:** #123

...
```
