---
description: Generate and run a coder-eval activation suite for a Claude Code skill — does the agent actually engage it when it should, and leave it alone when it shouldn't? Use when the user asks whether a skill triggers, wants to test skill activation, or worries a skill has silently stopped firing.
allowed-tools: ["Read", "Glob", "Grep", "Write", "Bash"]
---

# Skill activation check

A skill only earns its keep if the model reaches for it at the right moment. That
decision is made almost entirely from the skill's frontmatter `description` — so
"does my skill trigger?" is a measurable question, and this is how you measure it:
build a labelled set of user requests, run a real agent against each one, and score
whether the skill was engaged.

The user's request is: `$ARGUMENTS`

## Step 1 — Locate the target skill

`$ARGUMENTS` may be a path to a `SKILL.md`, a skill name, or empty.

- Empty: glob `.claude/skills/*/SKILL.md` and `**/skills/*/SKILL.md`. One match →
  use it. Several → list them and ask which. None → say so and stop.
- A name: find the matching skill directory.

The **bare skill name is the directory name** containing `SKILL.md`. Read the
frontmatter `description` and keep it in front of you: that string is what the model
matches against, so it is the primary input to row design and the thing you will end
up recommending edits to.

If the skill has **no `description`** in its frontmatter, stop and report that as the
finding — a skill with no description can never be model-invoked, so a suite would
score zero recall by construction and tell you nothing you don't already know.

## Step 2 — Confirm coder-eval is installed

Run `coder-eval --version`. If it is missing, tell the user to install it and stop:

```bash
uv tool install coder-eval    # or: pip install coder-eval
```

## Step 3 — Design the rows

This step is the whole experiment. The rest is mechanics.

**Positive rows** — requests a real user would plausibly make that the description
claims to cover. Paraphrase; never copy phrasing out of the description. A row lifted
from the description tests string matching, not activation. Vary the vocabulary and
include at least one oblique row where the user describes their *problem* rather than
the operation the skill performs.

**Distractor rows** — adjacent requests the skill should *not* claim, especially ones
that share vocabulary with the description. These are what make precision meaningful.

**Never name the skill in a prompt.** That tests obedience, not activation:

- Bad: "Use the pdf-forms skill to fill in this application."
- Good: "I need to fill in the fields on this application PDF and send it back."

**Sizing.** Minimum 3 positive + 3 distractor. Aim for **8–12 of each** for a signal
you can act on — recall over 3 rows moves in 33-point jumps, which is too coarse to
tell a real regression from noise. The shipped template holds 6 rows because that is
the illustrative minimum, not a target.

**Refuse to generate a suite with no distractor rows.** With no negatives, precision
is 1.0 by definition and half the result is meaningless. Say why and ask for the
adjacent cases instead.

## Step 4 — Write the suite

Copy the two template files into the user's task directory (`tasks/` if it exists,
otherwise ask):

- `${CLAUDE_PLUGIN_ROOT}/reference/templates/activation.yaml`
- `${CLAUDE_PLUGIN_ROOT}/reference/templates/activation-rows.jsonl`

Then substitute, keeping the JSONL beside the YAML (`dataset.paths` entries resolve
relative to the task file):

- `task_id` → `<skill-name>-activation`
- `skill_name` → the **bare** skill name
- each positive row's `expected_skill` → the same bare name; distractor rows keep `""`
- every row's `prompt` → the requests designed in step 3, one JSON object per line

`skill_name` must be the bare name even when the skill comes from a plugin and is
invoked as `plugin:skill` — the checker strips the namespace before comparing. A
namespaced value here silently scores zero recall on every row, which reads exactly
like a broken skill.

For criterion fields beyond this template, read
`${CLAUDE_PLUGIN_ROOT}/reference/criteria.md`.

## Step 5 — Validate before spending anything

`coder-eval plan <path-to-activation.yaml>` must exit 0. It is a schema check only —
it does not read the dataset file — so also confirm the JSONL sits next to the YAML
and has one object per line.

## Step 6 — Run

Every row is a full agent run: **N rows means N runs and real token cost**. State the
row count and the agent/model that will be used, then ask before starting.

```bash
coder-eval run <path-to-activation.yaml>
```

The criterion is agent-agnostic — it detects Claude engaging the skill via the `Skill`
tool, and any agent without that tool (Codex, for instance) by reading the skill's
files off disk — so the same suite works whichever agent the task resolves to.

## Step 7 — Report and interpret

Present recall, precision, F1 and the confusion matrix, then say what they mean:

- **Low recall** (misses rows it should have caught): the description under-claims or
  is too vague. It does not name the situations, file types, or phrasings that should
  trigger it.
- **Low precision** (fires on distractors): the description over-claims and is
  stealing adjacent requests. Narrow it, and say explicitly what the skill is *not*
  for.
- **Both high**: report the numbers and the row count, and note that a small suite
  says little — offer to widen it.

Point at the frontmatter `description` as the thing to edit, quote the specific rows
that failed as evidence, and offer to re-run after the edit so the change is measured
rather than assumed. Re-running the same suite after a description change is the whole
point: it turns skill wording from taste into a number that moves.
