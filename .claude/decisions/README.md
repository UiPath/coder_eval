# Decision log

Three registers for three different kinds of sentence. Putting them in one place is what made
`optimize/`'s docstrings 40-line essays, and an essay is not read: the contract a caller needs is
buried in the history of how the code got there.

| Register | Lives in | Answers |
|---|---|---|
| **Contract** | the docstring | What does this do, what does it return, what invariants hold? |
| **Why not the obvious alternative** | a short comment at the decision site | Why is this line shaped like this and not the way you were about to change it to? |
| **Defect history** | a dated file here | What broke once, how was it measured, and what did the fix cost? |

## The rule

A docstring keeps what a CALLER needs to use the function correctly. Everything that is only
interesting once you are changing it — the measured failure, the rejected design, the commit that
introduced the bug — moves here, and the code keeps a one-line pointer:

```python
# See .claude/decisions/<slug>.md
```

The pointer is not optional. `tests/lint_tests/test_lint_decision_log.py` asserts that every file
here is referenced from `src/`, because a decision nobody can find from the code is a decision
nobody will read — and it will be re-litigated.

## What does NOT go here

- **Deferred guardrails.** Those are `.claude/harness-candidates.md`: a candidate is work not yet
  done, a decision here is work settled. Mixing them was considered and rejected — a reader
  scanning for "what should I build next" would have to skip past history to find it.
- **Anything a sensor asserts.** If a test binds a sentence, that sentence stays where the test
  reads it. Moving it is not a documentation change, it is deleting a check.
- **The contract itself.** If trimming a docstring leaves a caller unable to use the function, the
  line was contract and belongs in the docstring. Say so rather than moving it; the ratchet in
  `test_lint_decision_log.py` has slack for exactly that case.

## Why this convention has no lint rule

"Is this sentence a contract or a defect history" is a semantic judgement, and a heuristic for it
would be a rule policing wording. What IS checked is mechanical: that no decision file is orphaned,
and that the number of over-long docstrings does not grow. This is the one invariant in the
`optimize/` family with no computational guardrail, and that is stated here rather than left to be
discovered.

## Naming

`YYYY-MM-DD-<slug>.md`, dated when the decision was recorded rather than when the defect shipped.
One file per subject, not per defect: a reader arrives from a pointer in one function and usually
needs the neighbouring history too.
