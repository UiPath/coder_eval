---
description: >-
  Step-by-step Coder Eval tutorials — run your first AI coding-agent evaluation,
  wire it into CI, browse results, write tasks, compare models, and isolate runs
  in Docker.
---

# Tutorials

Step-by-step, task-oriented walkthroughs. Start here if you're new; for the
complete command/config reference see the [User Guide](../USER_GUIDE.md), and for
the task-file schema see the [Task Definition Guide](../TASK_DEFINITION_GUIDE.md).

| # | Tutorial | You'll learn |
| - | --- | --- |
| 01 | [Your first evaluation](01-first-evaluation.md) | Install, configure a key, and run + read your first eval |
| 02 | [Running Coder Eval in CI](02-ci-pipeline.md) | Wire the suite into GitHub Actions with a pass/fail gate |
| 03 | [Browsing results with evalboard](03-evalboard-local.md) | Explore runs in a local web UI — pass rates, timelines, artifacts |
| 04 | [Writing a task](04-writing-a-task.md) | Author a task YAML with success criteria from scratch |
| 05 | [Comparing two models](05-comparing-models.md) | Use the experiment layer to A/B two configurations |
| 06 | [Running tasks in Docker isolation](06-use-docker-isolation.md) | Run each task in a fresh container; add task-specific dependencies |
| 07 | [Driving Coder Eval from Claude Code](07-plugin-in-claude-code.md) | Install the plugin and drive the whole loop — scaffold, author, review, run, analyze — from slash commands |
| 08 | [Optimizing a Skill Description](08-optimizing-a-skill.md) | Measure whether a skill's description can be improved — tune/holdout splits, the reachability check that decides everything, and when to stop |

> Contributions welcome — add a numbered `NN-title.md` file and link it in the
> table above. Keep tutorials short, copy-pasteable, and outcome-focused.
