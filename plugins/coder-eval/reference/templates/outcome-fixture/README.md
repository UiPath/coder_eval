# REPLACE: the starting repository every row works from

This directory is the fixture `outcome.yaml` mounts. **Everything in it is copied into every
sandbox, for every row of every arm** — including this file, which announces to the agent that it
is inside an eval. **Delete it** once the real starting state is here, and read
`outcome.yaml`'s `sandbox:` comment before adding anything: a grader's expectations placed here
hand the agent what it is being marked against, and the run still looks completely normal.

It ships with one placeholder file rather than empty because an empty directory cannot be
committed, and because a mounted directory that does not exist fails `coder-eval plan` — which is
the check that would otherwise tell you nothing until the run had already started.
