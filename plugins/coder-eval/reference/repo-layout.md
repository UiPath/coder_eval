# Finding a repository's eval tree

Read by every skill that has to locate this repository's tasks, runs or experiments.
There is no fixed location. One repository keeps `tasks/` at the root, another calls it
`evals/` or `benchmarks/`, a third nests the whole tree under its test directory. So a
skill **discovers** the tree and **reports what it resolved** — it never assumes a path.

## Discover by content, not by directory name

- **Tasks** — glob `**/*.yaml` and keep the files carrying a `task_id:` key. That key is
  what makes a file a task. A directory named `tasks/` holding only fixtures is not a task
  tree, and a directory named anything at all holding `task_id:` files is.
- **Runs** — glob `**/run.json`. Each match is a run root.
- **Experiments** — YAML carrying a `variants:` key, usually a sibling of the task tree.

Prune `node_modules`, `.venv`, `.git`, `dist` and `build` from every glob. This is not
optional: on a large repository an unpruned recursive glob is slow enough to look like a
hang, and it promotes vendored fixtures into candidate eval trees.

**Directory names are not the signal.** Keying on `tasks/` or `runs/` would swap one
hardcoded assumption for a wider one. A name is a **tiebreaker only**: when several
candidates are otherwise equal, prefer the one literally named `tasks/` or `runs/`, and
say that is why you picked it.

## Report what you resolved, then act

Before doing anything with what you found, tell the user:

- the root you resolved, and that you **discovered** it rather than assumed it;
- how many task files and run directories sit under it.

Then:

- **Exactly one candidate** → use it.
- **Several** — a monorepo with two eval trees — → list them and ask. Never silently take
  the first.
- **None** → say exactly what you globbed and where, and stop. An empty result is an
  error, never a clean pass.

## Runs, and the `latest` pointer

A run root holds one directory per run, named after its run id, which sorts
chronologically. Many repositories also keep a `latest` symlink beside them.

When you need "the most recent run" and were given no path: resolve the run root as
above, then use `latest` **if that symlink exists and resolves**; otherwise take the
newest run directory by name. Either way say which one you picked and how. A dangling
`latest` is a finding worth reporting, not something to read through.

## Passing paths to the CLI

**Always pass the discovered paths explicitly** — `coder-eval plan <paths>`,
`coder-eval run <paths>`. Never suggest running those commands with no path argument:
zero-argument discovery resolves against the installed package's own location, so from a
normally installed CLI it searches the install directory, finds nothing, and exits 1.

## An example, and only an example

A repository whose eval tree lives under its test directory:

```
tests/
  tasks/          # *.yaml files carrying task_id:
  runs/           # one directory per run, each with run.json
  experiments/    # variant definitions
```

That is one repository's layout, shown to make the shape concrete. It is **not** a
default and **not** a fallback — discover the tree; do not go looking for this one.
