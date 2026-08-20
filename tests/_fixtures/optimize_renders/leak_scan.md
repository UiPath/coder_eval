**Verbatim train-leak scan.**

- `1-a-widen-vocabulary` — 1 span(s) the baseline does not have:
    - my-suite/r1: candidate adds 'minimum-task-score' (file_check)
- `1-b-name-the-symptom` — clean.

Not scanned, by design: `1-control`, `1-incumbent`. The baseline is what every candidate is diffed AGAINST, and the control arm is not a candidate.

**Verbatim only, so clean is not a proof.** This finds a train row's graded string reproduced character for character in a candidate's own files, diffed against the baseline it was edited from. A candidate that PARAPHRASES a train row, or encodes an answer structurally, is invisible here — that is a semantic leak and it still needs reading. A hit is a reason to rewrite the span, not to abandon the candidate: replace it with a placeholder, or generalize it to the category of request it belongs to.