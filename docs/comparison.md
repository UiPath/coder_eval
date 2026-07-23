---
title: "Coder Eval vs. SWE-bench, SkillsBench, Harbor & OpenAI Evals — how it compares"
description: >-
  How Coder Eval compares to SWE-bench, SkillsBench, Harbor, and OpenAI Evals for
  evaluating AI coding agents and Claude Code skills — with when to choose each.
  Grounded in each project's own docs.
---

# How Coder Eval compares

**Coder Eval** is a framework for evaluating AI coding agents and their skills. It
runs an agent (Claude Code, Codex, Gemini) in a sandbox against declarative YAML
tasks and scores the files and commands it produces on a weighted 0.0–1.0 scale,
with cost/token telemetry, an A/B experiment layer, skill-activation checks, and CI
pass/fail gates.

The tools below overlap with parts of that scope. This page describes what each is
designed for and when to choose it. Every claim about another project is grounded
in its own documentation; see [Sources](#sources).

## At a glance

| | Coder Eval | SWE-bench | SkillsBench | Harbor | OpenAI Evals | Hand-rolled |
| --- | --- | --- | --- | --- | --- | --- |
| **What it grades** | Files + commands the agent produced | Whether a patch passes the repo's tests | Skill value on a fixed task set | Agent task success in sandboxes | Model text output | Whatever you wire up |
| **Task source** | Your own (YAML) | Fixed benchmark (+ collection script) | Fixed (87 tasks / 8 domains) | Your own (framework) + Terminal-Bench 2.0 | Your own (YAML/JSON) + registry | Manual |
| **Runs a real agent + tools** | ✅ Claude Code, Codex, Gemini | Runs your patch/scaffold | ✅ multi-harness | ✅ Claude Code, OpenHands, Codex | ❌ grades model output | ❌ |
| **Sandboxed & reproducible** | ✅ tempdir / Docker | ✅ Docker | ✅ deterministic verifiers | ✅ Docker + cloud (Daytona/Modal) | — (grades text) | ❌ |
| **Scoring** | Weighted 0.0–1.0 + thresholds | Pass/fail (tests) | Pass/fail (verifiers) | Task-level | Match / model-graded | ❌ |
| **A/B experiments (model / tool / prompt)** | ✅ built-in | ❌ | Skills on/off only | — | Compare model versions | ❌ |
| **Cost / token telemetry** | ✅ per tool call | ❌ | ❌ | — | limited | ❌ |
| **Skill-activation testing** | ✅ `skill_triggered` | ❌ | Measures effect, not trigger | ❌ | ❌ | ❌ |
| **CI pass/fail gate** | ✅ built-in | ❌ | ❌ | ❌ | ❌ | ❌ |
| **Results dashboard** | ✅ evalboard | ❌ | ❌ | ❌ | ❌ | ❌ |
| **Primary interface** | Python + YAML | Python harness + HF dataset | Research benchmark | Python CLI | Python + YAML/JSON | Your choice |

## vs. SWE-bench and fixed benchmarks

[SWE-bench](https://www.swebench.com/) is a fixed benchmark of real GitHub issues.
A model generates a patch, which is graded by running the repository's tests in
Docker; SWE-bench Verified is a human-validated subset of 500 problems, and there
are additional variants (Lite, Multimodal, Multilingual). It includes a
data-collection procedure for building tasks from other repositories, but its
primary use is a shared leaderboard on a canonical dataset.

Coder Eval is a framework for authoring tasks in YAML, with continuous 0.0–1.0
scoring and cost telemetry rather than a single pass/fail. A fixed dataset can be
wrapped as tasks via [Bring Your Own Dataset](BYOD.md).

**Choose SWE-bench** for a standardized model leaderboard on patch-and-test tasks.
**Choose Coder Eval** to evaluate your own tasks, agents, and skills and to gate CI
on them.

## vs. SkillsBench and skill benchmarks

[SkillsBench](https://arxiv.org/abs/2602.12670) is a fixed research benchmark for
agent skills: 87 tasks across 8 domains, each paired with curated skills and
deterministic verifiers. It compares agents with and without the curated skills
across 18 model-harness configurations and reports a 33.9% → 50.5% average
pass-rate change. It measures whether skills improve performance on that task set.

Coder Eval addresses a related but distinct question about a specific set of skills:

- **Activation vs. effectiveness.** Coder Eval's `skill_triggered` criterion checks
  whether the agent engaged the skill (Claude's `Skill` tool call, or Codex reading
  the skill's files). SkillsBench measures the downstream effect on task success.
  The two are different signals: a skill that does not activate cannot contribute.
- **Reusable in CI.** A skill-on/off comparison can be built with the
  [experiment layer](AB_EXPERIMENTS.md), gated on each change, and scheduled to
  detect regressions over time.

**Choose SkillsBench** for a standardized measure of skill value across domains.
**Choose Coder Eval** to author, activation-test, and regression-check your own
skills.

## vs. Harbor

[Harbor](https://www.harborframework.com/) is a Python framework from the
Terminal-Bench team for evaluating and optimizing agents and language models. It
runs agents (Claude Code, OpenHands, Codex CLI) in sandboxes using Docker and cloud
providers (Daytona, Modal, LangSmith), is the official harness for Terminal-Bench
2.0, can run across large numbers of environments in parallel, and can generate
rollouts for RL optimization. Its emphasis is scale and optimization.

Coder Eval focuses on scoring and workflow for coding-agent and skill suites:

- 14 weighted criterion types (file/regex/JSON checks, `run_command`, AST/token
  similarity, LLM- and agent-judges) on a 0.0–1.0 scale with per-criterion
  thresholds.
- An [experiment layer](AB_EXPERIMENTS.md) for model/tool/prompt A/Bs,
  `skill_triggered` activation checks, per-tool cost/token telemetry, CI pass/fail
  gates, and the evalboard dashboard.

**Choose Harbor** for large-scale agent evaluation and RL-optimization rollouts,
particularly around Terminal-Bench. **Choose Coder Eval** for weighted, skill-aware
task suites gated in CI.

## vs. OpenAI Evals

[OpenAI Evals](https://github.com/openai/evals) is a framework and registry for
evaluating model output. Data is provided in JSON and parameters in YAML, and
grading uses exact/pattern match or model-graded templates. It evaluates model
output rather than running an autonomous coding agent with tool use in a sandbox,
and contributions of custom-code evals are restricted (model-graded YAML evals are
the accepted path).

Coder Eval runs the full agent and scores the files and commands it produces, with
per-tool cost/token telemetry and skill-activation checks.

**Choose OpenAI Evals** to grade model output and compare model versions. **Choose
Coder Eval** to evaluate agent behavior — the tools it calls, the files it writes,
the commands it runs, and the skills it triggers.

## vs. hand-rolled scripts

A `claude -p` loop is quick to start, but a durable harness requires implementing
sandboxing, weighted criteria, retries, cost/token accounting, A/B configuration, a
results view, and CI reporting. Coder Eval provides these components, so work can
focus on tasks and criteria rather than harness infrastructure.

## When to choose Coder Eval

- Evaluating how well agents use a CLI or Claude Code skills.
- A/B-testing models, tools, or prompts on the same tasks (Claude Code vs. Codex vs. Gemini).
- Detecting skill regressions when a model or prompt changes.
- Gating CI on coding-agent quality.
- Producing reproducible, sandboxed runs scored per file and per command, with a dashboard for review.

## Next steps

- [Tutorial 01 — Your First Evaluation](tutorials/01-first-evaluation.md)
- [Tutorial 02 — Running Coder Eval in CI](tutorials/02-ci-pipeline.md)
- [A/B Experiments](AB_EXPERIMENTS.md)
- [Bring Your Own Dataset](BYOD.md)

## Sources

Claims about other projects are grounded in their own documentation, retrieved
July 2026:

- SWE-bench — [swebench.com](https://www.swebench.com/) and the
  [SWE-bench repository](https://github.com/SWE-bench/SWE-bench) (real GitHub
  issues, Docker-reproducible test-based evaluation, Verified = 500 problems).
- SkillsBench — [*Benchmarking How Well Agent Skills Work Across Diverse Tasks*](https://arxiv.org/abs/2602.12670)
  (87 tasks / 8 domains, deterministic verifiers, 18 model-harness configs,
  33.9% → 50.5% pass-rate lift).
- Harbor — [harborframework.com](https://www.harborframework.com/) and the
  [harbor-framework/harbor repository](https://github.com/harbor-framework/harbor)
  (Python framework from the Terminal-Bench creators; Docker + multi-cloud sandboxes;
  official Terminal-Bench 2.0 harness; parallel across thousands of environments;
  RL-optimization rollouts).
- OpenAI Evals — [openai/evals](https://github.com/openai/evals) (framework +
  registry; JSON data + YAML params; match and model-graded evals).
