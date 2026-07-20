# CHANGELOG

<!-- version list -->

## v0.8.6 (2026-07-20)

### Features

- Add Sonnet 5 + GPT-5.6 pricing; default antigravity to gemini-3.5-flash
  ([#31](https://github.com/UiPath/coder_eval/pull/31),
  [`4c40acd`](https://github.com/UiPath/coder_eval/commit/4c40acd64482e9ffcec66b61ac2aab57b5c89f6f))


## v0.8.5 (2026-07-20)

### Bug Fixes

- **deps**: Bump mcp to >=1.28.1 for CVE-2026-52869/52870/59950
  ([#25](https://github.com/UiPath/coder_eval/pull/25),
  [`fb1ad4c`](https://github.com/UiPath/coder_eval/commit/fb1ad4cba2973414a6a883468f62c908deee532b))

- **orchestration**: Isolate per-task config-resolution failures from the suite
  ([#25](https://github.com/UiPath/coder_eval/pull/25),
  [`fb1ad4c`](https://github.com/UiPath/coder_eval/commit/fb1ad4cba2973414a6a883468f62c908deee532b))

- **orchestration**: Normalize the all-fail config-resolution abort to ValueError
  ([#25](https://github.com/UiPath/coder_eval/pull/25),
  [`fb1ad4c`](https://github.com/UiPath/coder_eval/commit/fb1ad4cba2973414a6a883468f62c908deee532b))

- **orchestration**: Re-raise ValueError verbatim in the all-fail abort
  ([#25](https://github.com/UiPath/coder_eval/pull/25),
  [`fb1ad4c`](https://github.com/UiPath/coder_eval/commit/fb1ad4cba2973414a6a883468f62c908deee532b))

- **orchestrator**: Interrupt-proof teardown so a timeout can't drop task.json
  ([#29](https://github.com/UiPath/coder_eval/pull/29),
  [`89ec0d0`](https://github.com/UiPath/coder_eval/commit/89ec0d0eda74616d21a87302680e3099473119bc))

- **sandbox**: Prune capture-ignored entries on every preservation path; interrupt-proof teardown
  ([#29](https://github.com/UiPath/coder_eval/pull/29),
  [`89ec0d0`](https://github.com/UiPath/coder_eval/commit/89ec0d0eda74616d21a87302680e3099473119bc))

### Chores

- **evalboard**: Address npm Dependabot alerts ([#18](https://github.com/UiPath/coder_eval/pull/18),
  [`3d5e7b7`](https://github.com/UiPath/coder_eval/commit/3d5e7b7d2358e10d95d59487c54596df7da1bef2))

- **evalboard**: Batch github-actions bumps into one grouped PR
  ([#18](https://github.com/UiPath/coder_eval/pull/18),
  [`3d5e7b7`](https://github.com/UiPath/coder_eval/commit/3d5e7b7d2358e10d95d59487c54596df7da1bef2))

### Documentation

- **orchestrator**: Correct teardown comment to scope of the fix
  ([#29](https://github.com/UiPath/coder_eval/pull/29),
  [`89ec0d0`](https://github.com/UiPath/coder_eval/commit/89ec0d0eda74616d21a87302680e3099473119bc))

### Features

- **evalboard**: Show conversation transcript for simulation tasks
  ([#23](https://github.com/UiPath/coder_eval/pull/23),
  [`252722a`](https://github.com/UiPath/coder_eval/commit/252722ab17edf3dbc1b68a7ca3a357b2dd3cb0d3))

### Refactoring

- **orchestrator**: Trim to interrupt-proof teardown; drop preservation-prune
  ([#29](https://github.com/UiPath/coder_eval/pull/29),
  [`89ec0d0`](https://github.com/UiPath/coder_eval/commit/89ec0d0eda74616d21a87302680e3099473119bc))


## v0.8.4 (2026-07-13)

### Bug Fixes

- **deps**: Upgrade click to 8.4.2 to resolve PYSEC-2026-2132
  ([#19](https://github.com/UiPath/coder_eval/pull/19),
  [`a240d4e`](https://github.com/UiPath/coder_eval/commit/a240d4e2f8b970665743d0dc3d212c6c8e8a30ac))

- **deps**: Upgrade click to 8.4.2 to resolve PYSEC-2026-2132
  ([#20](https://github.com/UiPath/coder_eval/pull/20),
  [`bba8645`](https://github.com/UiPath/coder_eval/commit/bba8645fc3f6c7b6e9784af496641ac0eaf8bede))

- **evalboard**: Address search-box code review comments
  ([#13](https://github.com/UiPath/coder_eval/pull/13),
  [`382d80d`](https://github.com/UiPath/coder_eval/commit/382d80d4a84da39327b0196c97c01a367f094c0e))

- **evalboard**: Fix search bar clear issue ([#13](https://github.com/UiPath/coder_eval/pull/13),
  [`382d80d`](https://github.com/UiPath/coder_eval/commit/382d80d4a84da39327b0196c97c01a367f094c0e))

- **evalboard**: Prevent search bar from resetting mid-type
  ([#13](https://github.com/UiPath/coder_eval/pull/13),
  [`382d80d`](https://github.com/UiPath/coder_eval/commit/382d80d4a84da39327b0196c97c01a367f094c0e))

- **sandbox**: Exclude home-dir dotfiles from capture_to artifacts
  ([#19](https://github.com/UiPath/coder_eval/pull/19),
  [`a240d4e`](https://github.com/UiPath/coder_eval/commit/a240d4e2f8b970665743d0dc3d212c6c8e8a30ac))

- **sandbox**: Extend capture_to denylist with credential stores
  ([#19](https://github.com/UiPath/coder_eval/pull/19),
  [`a240d4e`](https://github.com/UiPath/coder_eval/commit/a240d4e2f8b970665743d0dc3d212c6c8e8a30ac))

### Code Style

- Fix ruff formatting in _WORKSPACE_CAPTURE_IGNORE
  ([#19](https://github.com/UiPath/coder_eval/pull/19),
  [`a240d4e`](https://github.com/UiPath/coder_eval/commit/a240d4e2f8b970665743d0dc3d212c6c8e8a30ac))

### Documentation

- **readme**: Clarify framing before hero gif ([#16](https://github.com/UiPath/coder_eval/pull/16),
  [`b7dee1c`](https://github.com/UiPath/coder_eval/commit/b7dee1c0616d9a98dc98432ca066c1a8c9264ef4))

- **readme**: Reframe title toward agents & their skills
  ([#16](https://github.com/UiPath/coder_eval/pull/16),
  [`b7dee1c`](https://github.com/UiPath/coder_eval/commit/b7dee1c0616d9a98dc98432ca066c1a8c9264ef4))

### Features

- **early-stop**: Opt-in early stop once armed criteria are decided
  ([#14](https://github.com/UiPath/coder_eval/pull/14),
  [`b0c1ade`](https://github.com/UiPath/coder_eval/commit/b0c1ade364119573f172e4d64ee3fff0a387db32))

### Testing

- **sandbox**: Cover home-dir dotfile exclusion in capture_to
  ([#19](https://github.com/UiPath/coder_eval/pull/19),
  [`a240d4e`](https://github.com/UiPath/coder_eval/commit/a240d4e2f8b970665743d0dc3d212c6c8e8a30ac))


## v0.8.3 (2026-07-09)

### Bug Fixes

- **evalboard**: Fall back to agent_config type when run_config omits harness
  ([#10](https://github.com/UiPath/coder_eval/pull/10),
  [`59a240c`](https://github.com/UiPath/coder_eval/commit/59a240c3d87d3ec4aa7f43a77673dd0561a0e006))

### Continuous Integration

- Remove Azure Artifacts publishing ([#11](https://github.com/UiPath/coder_eval/pull/11),
  [`2a5124f`](https://github.com/UiPath/coder_eval/commit/2a5124f2c2c8b4151b83a2a9ff2c0bc464683a34))

- Restore auto-bump release (semantic-release + app token)
  ([#12](https://github.com/UiPath/coder_eval/pull/12),
  [`0b9d378`](https://github.com/UiPath/coder_eval/commit/0b9d378e5a99a878a211046a9a9a9ab7f40faaa4))

### Features

- **evalboard**: Add a Harness (RunConfig) column to the runs tables
  ([#10](https://github.com/UiPath/coder_eval/pull/10),
  [`59a240c`](https://github.com/UiPath/coder_eval/commit/59a240c3d87d3ec4aa7f43a77673dd0561a0e006))

- **evalboard**: Add Gemini rates to the frontend pricing table
  ([#10](https://github.com/UiPath/coder_eval/pull/10),
  [`59a240c`](https://github.com/UiPath/coder_eval/commit/59a240c3d87d3ec4aa7f43a77673dd0561a0e006))

- **evalboard**: Show harness as a vendor logo (internal, main table only)
  ([#10](https://github.com/UiPath/coder_eval/pull/10),
  [`59a240c`](https://github.com/UiPath/coder_eval/commit/59a240c3d87d3ec4aa7f43a77673dd0561a0e006))

- **evalboard**: Show the harness (RunConfig) column on the runs tables
  ([#10](https://github.com/UiPath/coder_eval/pull/10),
  [`59a240c`](https://github.com/UiPath/coder_eval/commit/59a240c3d87d3ec4aa7f43a77673dd0561a0e006))


## v0.8.2 (2026-07-07)

### Bug Fixes

- **antigravity**: Apply env_path_prepend so mock CLIs shadow real ones
  ([#487](https://github.com/UiPath/coder_eval/pull/487),
  [`412d28f`](https://github.com/UiPath/coder_eval/commit/412d28fab21221ce1d3d272f392fc87eee93fc33))

- **antigravity**: Take harness-spawn lock unconditionally so no-prepend spawns wait out
  mutated-PATH windows ([#487](https://github.com/UiPath/coder_eval/pull/487),
  [`412d28f`](https://github.com/UiPath/coder_eval/commit/412d28fab21221ce1d3d272f392fc87eee93fc33))

- **ci**: Restore claude-pr-review git-fetch auth broken by persist-credentials
  ([#485](https://github.com/UiPath/coder_eval/pull/485),
  [`155ecb2`](https://github.com/UiPath/coder_eval/commit/155ecb2b61cfd609b5b5e6dc8470536860fc42b1))

### Continuous Integration

- Address PR #485 review — helper host-scoping, test linkage, doc version drift
  ([#485](https://github.com/UiPath/coder_eval/pull/485),
  [`155ecb2`](https://github.com/UiPath/coder_eval/commit/155ecb2b61cfd609b5b5e6dc8470536860fc42b1))

### Documentation

- Add Docker isolation tutorial (04) + venv activation note
  ([#482](https://github.com/UiPath/coder_eval/pull/482),
  [`47b3e0b`](https://github.com/UiPath/coder_eval/commit/47b3e0bbbd9e2b05f340571869f1e4324d226424))

- Add docker isolation tutorials ([#482](https://github.com/UiPath/coder_eval/pull/482),
  [`47b3e0b`](https://github.com/UiPath/coder_eval/commit/47b3e0bbbd9e2b05f340571869f1e4324d226424))

- Optimize README + pyproject for discoverability
  ([#485](https://github.com/UiPath/coder_eval/pull/485),
  [`155ecb2`](https://github.com/UiPath/coder_eval/commit/155ecb2b61cfd609b5b5e6dc8470536860fc42b1))

- OSS discoverability + packaging metadata, and a claude-pr-review CI fix
  ([#485](https://github.com/UiPath/coder_eval/pull/485),
  [`155ecb2`](https://github.com/UiPath/coder_eval/commit/155ecb2b61cfd609b5b5e6dc8470536860fc42b1))

- OSS-readiness follow-ups — version drift, positioning, PyPI install
  ([#485](https://github.com/UiPath/coder_eval/pull/485),
  [`155ecb2`](https://github.com/UiPath/coder_eval/commit/155ecb2b61cfd609b5b5e6dc8470536860fc42b1))

- **antigravity**: Describe the PATH-mutation window as the full harness context-entry, not just the
  Popen ([#487](https://github.com/UiPath/coder_eval/pull/487),
  [`412d28f`](https://github.com/UiPath/coder_eval/commit/412d28fab21221ce1d3d272f392fc87eee93fc33))

- **tutorials**: Add a section about docker instalation
  ([#482](https://github.com/UiPath/coder_eval/pull/482),
  [`47b3e0b`](https://github.com/UiPath/coder_eval/commit/47b3e0bbbd9e2b05f340571869f1e4324d226424))

- **tutorials**: Changed some texts to make more clear
  ([#482](https://github.com/UiPath/coder_eval/pull/482),
  [`47b3e0b`](https://github.com/UiPath/coder_eval/commit/47b3e0bbbd9e2b05f340571869f1e4324d226424))

- **tutorials**: Fix links ([#482](https://github.com/UiPath/coder_eval/pull/482),
  [`47b3e0b`](https://github.com/UiPath/coder_eval/commit/47b3e0bbbd9e2b05f340571869f1e4324d226424))

- **tutorials**: Fix stale uipath-credentials claim + review nits
  ([#482](https://github.com/UiPath/coder_eval/pull/482),
  [`47b3e0b`](https://github.com/UiPath/coder_eval/commit/47b3e0bbbd9e2b05f340571869f1e4324d226424))

- **tutorials**: Rename docker tutorials ([#482](https://github.com/UiPath/coder_eval/pull/482),
  [`47b3e0b`](https://github.com/UiPath/coder_eval/commit/47b3e0bbbd9e2b05f340571869f1e4324d226424))

### Refactoring

- **antigravity**: Type the spawn-lock loop global as AbstractEventLoop | None instead of Any
  ([#487](https://github.com/UiPath/coder_eval/pull/487),
  [`412d28f`](https://github.com/UiPath/coder_eval/commit/412d28fab21221ce1d3d272f392fc87eee93fc33))

### Testing

- **antigravity**: Cover PATH restore when the spawn guard body raises
  ([#487](https://github.com/UiPath/coder_eval/pull/487),
  [`412d28f`](https://github.com/UiPath/coder_eval/commit/412d28fab21221ce1d3d272f392fc87eee93fc33))


## v0.8.1 (2026-07-06)

### Bug Fixes

- Address PR #468 review — OSS-prep docs/CI/packaging papercuts
  ([#468](https://github.com/UiPath/coder_eval/pull/468),
  [`7e75cd8`](https://github.com/UiPath/coder_eval/commit/7e75cd86712966b17a16035b5b0328d1388dbcb6))

- Code review fixes for open-source-docs-cleanup
  ([#481](https://github.com/UiPath/coder_eval/pull/481),
  [`1810741`](https://github.com/UiPath/coder_eval/commit/18107418f8c5d129b27834479f2e90218795cb85))

- Results of Fable code review: discriminated unions, judge retry/ERROR escalation, DEFAULT_*
  removal, resilience fixes ([#483](https://github.com/UiPath/coder_eval/pull/483),
  [`6d2564f`](https://github.com/UiPath/coder_eval/commit/6d2564ff5bbf255a3c8f4c533572dc3058b5cb9c))

- **agents**: Address Antigravity review — loud turn-status conversion + test/lint hardening
  ([#461](https://github.com/UiPath/coder_eval/pull/461),
  [`74e4774`](https://github.com/UiPath/coder_eval/commit/74e4774c52a7e3b2d0e91413d03379ca6ff3b81b))

- **agents**: Let Antigravity read skill files inside workspace_only sandbox
  ([#461](https://github.com/UiPath/coder_eval/pull/461),
  [`74e4774`](https://github.com/UiPath/coder_eval/commit/74e4774c52a7e3b2d0e91413d03379ca6ff3b81b))

- **agents**: Normalize Antigravity tool-call params to canonical keys
  ([#461](https://github.com/UiPath/coder_eval/pull/461),
  [`74e4774`](https://github.com/UiPath/coder_eval/commit/74e4774c52a7e3b2d0e91413d03379ca6ff3b81b))

- **agents**: Silence pyright on optional google-antigravity import
  ([#461](https://github.com/UiPath/coder_eval/pull/461),
  [`74e4774`](https://github.com/UiPath/coder_eval/commit/74e4774c52a7e3b2d0e91413d03379ca6ff3b81b))

- **ci**: Close token-exfil + comment-injection gaps in claude-pr-review
  ([#472](https://github.com/UiPath/coder_eval/pull/472),
  [`9e374de`](https://github.com/UiPath/coder_eval/commit/9e374de64a89e9d8e4d87b3e25b2ab17b654d55a))

- **ci**: Harden claude-pr-review with tool + comment allowlists
  ([#472](https://github.com/UiPath/coder_eval/pull/472),
  [`9e374de`](https://github.com/UiPath/coder_eval/commit/9e374de64a89e9d8e4d87b3e25b2ab17b654d55a))

- **codex**: Apply env_path_prepend to app-server PATH so mock CLIs shadow real ones
  ([#480](https://github.com/UiPath/coder_eval/pull/480),
  [`8a53bbe`](https://github.com/UiPath/coder_eval/commit/8a53bbe024fb628d4fd16c88b4564d9c1ba09562))

- **docker**: Real multi-stage runtime kit + drop unused label (review)
  ([#466](https://github.com/UiPath/coder_eval/pull/466),
  [`15712a6`](https://github.com/UiPath/coder_eval/commit/15712a6c74cb3a12032e5ac9035dff1f0bcb552d))

- **errors**: 6/6 — categorization fall-through + sandbox-cleanup guard
  ([#483](https://github.com/UiPath/coder_eval/pull/483),
  [`6d2564f`](https://github.com/UiPath/coder_eval/commit/6d2564ff5bbf255a3c8f4c533572dc3058b5cb9c))

- **evalboard**: Collapsed replicate row shows Passed if any replicate passed
  ([#474](https://github.com/UiPath/coder_eval/pull/474),
  [`caa9cee`](https://github.com/UiPath/coder_eval/commit/caa9ceeacc59b362b3e7b4dae995bf751d3cbcde))

- **evalboard**: Colour all failure statuses red, not grey
  ([#474](https://github.com/UiPath/coder_eval/pull/474),
  [`caa9cee`](https://github.com/UiPath/coder_eval/commit/caa9ceeacc59b362b3e7b4dae995bf751d3cbcde))

- **evalboard**: Reconcile window-summary Runs denominator and share passClass
  ([#477](https://github.com/UiPath/coder_eval/pull/477),
  [`936731b`](https://github.com/UiPath/coder_eval/commit/936731b2f799f450070fb7d935ebb2b0b3dcf93e))

### Chores

- **docs**: 1/3 — delete internal doc trees, CLA, and feature-doc process
  ([#481](https://github.com/UiPath/coder_eval/pull/481),
  [`1810741`](https://github.com/UiPath/coder_eval/commit/18107418f8c5d129b27834479f2e90218795cb85))

### Code Style

- **samples**: Restore upstream curly quotes in pddl prompt
  ([#473](https://github.com/UiPath/coder_eval/pull/473),
  [`6bc706f`](https://github.com/UiPath/coder_eval/commit/6bc706fd3249a16de20bbe52728c10d8ecd2b868))

### Documentation

- 2/3 — purge all dangling references to the deleted docs/features tree
  ([#481](https://github.com/UiPath/coder_eval/pull/481),
  [`1810741`](https://github.com/UiPath/coder_eval/commit/18107418f8c5d129b27834479f2e90218795cb85))

- Add Contributor License Agreement file and reference it in CONTRIBUTING
  ([#468](https://github.com/UiPath/coder_eval/pull/468),
  [`7e75cd8`](https://github.com/UiPath/coder_eval/commit/7e75cd86712966b17a16035b5b0328d1388dbcb6))

- Address PR #468 review round 2 — remove workflow residue from public tree
  ([#468](https://github.com/UiPath/coder_eval/pull/468),
  [`7e75cd8`](https://github.com/UiPath/coder_eval/commit/7e75cd86712966b17a16035b5b0328d1388dbcb6))

- Address PR #481 review — scrub residues, purge dangling refs
  ([#481](https://github.com/UiPath/coder_eval/pull/481),
  [`1810741`](https://github.com/UiPath/coder_eval/commit/18107418f8c5d129b27834479f2e90218795cb85))

- Defer type-Literal-default lint candidate from top5-review-fixes run
  ([#483](https://github.com/UiPath/coder_eval/pull/483),
  [`6d2564f`](https://github.com/UiPath/coder_eval/commit/6d2564ff5bbf255a3c8f4c533572dc3058b5cb9c))

- Open-source prep — community-health files, docs, and review fixes
  ([#468](https://github.com/UiPath/coder_eval/pull/468),
  [`7e75cd8`](https://github.com/UiPath/coder_eval/commit/7e75cd86712966b17a16035b5b0328d1388dbcb6))

- Open-source prep — delete internal doc trees, purge references, add tutorials 04/05
  ([#481](https://github.com/UiPath/coder_eval/pull/481),
  [`1810741`](https://github.com/UiPath/coder_eval/commit/18107418f8c5d129b27834479f2e90218795cb85))

- Sweep stale .env DEFAULT_* references after layer-5 removal
  ([#483](https://github.com/UiPath/coder_eval/pull/483),
  [`6d2564f`](https://github.com/UiPath/coder_eval/commit/6d2564ff5bbf255a3c8f4c533572dc3058b5cb9c))

- **samples**: Add step-by-step run guide to SkillsBench README
  ([#473](https://github.com/UiPath/coder_eval/pull/473),
  [`6bc706f`](https://github.com/UiPath/coder_eval/commit/6bc706fd3249a16de20bbe52728c10d8ecd2b868))

- **tasks**: Lead the tasks README with samples, sentinels below
  ([#473](https://github.com/UiPath/coder_eval/pull/473),
  [`6bc706f`](https://github.com/UiPath/coder_eval/commit/6bc706fd3249a16de20bbe52728c10d8ecd2b868))

- **tutorials**: 3/3 — add 04 Writing a task and 05 Comparing two models
  ([#481](https://github.com/UiPath/coder_eval/pull/481),
  [`1810741`](https://github.com/UiPath/coder_eval/commit/18107418f8c5d129b27834479f2e90218795cb85))

### Features

- **agents**: Add Antigravity (Gemini) backend via google-antigravity SDK
  ([#461](https://github.com/UiPath/coder_eval/pull/461),
  [`74e4774`](https://github.com/UiPath/coder_eval/commit/74e4774c52a7e3b2d0e91413d03379ca6ff3b81b))

- **agents**: Wire Antigravity skill discovery via native skills_paths
  ([#461](https://github.com/UiPath/coder_eval/pull/461),
  [`74e4774`](https://github.com/UiPath/coder_eval/commit/74e4774c52a7e3b2d0e91413d03379ca6ff3b81b))

- **config**: 5/6 — remove the .env DEFAULT_* layer-5 knobs
  ([#483](https://github.com/UiPath/coder_eval/pull/483),
  [`6d2564f`](https://github.com/UiPath/coder_eval/commit/6d2564ff5bbf255a3c8f4c533572dc3058b5cb9c))

- **docker**: Make docker-images — build agent + runtime kit in one command
  ([#466](https://github.com/UiPath/coder_eval/pull/466),
  [`15712a6`](https://github.com/UiPath/coder_eval/commit/15712a6c74cb3a12032e5ac9035dff1f0bcb552d))

- **docker**: Relocatable runtime kit for inject-mode tasks
  ([#466](https://github.com/UiPath/coder_eval/pull/466),
  [`15712a6`](https://github.com/UiPath/coder_eval/commit/15712a6c74cb3a12032e5ac9035dff1f0bcb552d))

- **evalboard**: K/N ✓ pass-count badge on replicated task rows
  ([#474](https://github.com/UiPath/coder_eval/pull/474),
  [`caa9cee`](https://github.com/UiPath/coder_eval/commit/caa9ceeacc59b362b3e7b4dae995bf751d3cbcde))

- **evalboard**: Per-task pass rate + k/N ✓ badge for replicated runs
  ([#474](https://github.com/UiPath/coder_eval/pull/474),
  [`caa9cee`](https://github.com/UiPath/coder_eval/commit/caa9ceeacc59b362b3e7b4dae995bf751d3cbcde))

- **evalboard**: Per-task pass rate across replicates
  ([#474](https://github.com/UiPath/coder_eval/pull/474),
  [`caa9cee`](https://github.com/UiPath/coder_eval/commit/caa9ceeacc59b362b3e7b4dae995bf751d3cbcde))

- **evalboard**: Window cost + run summary on the front page
  ([#477](https://github.com/UiPath/coder_eval/pull/477),
  [`936731b`](https://github.com/UiPath/coder_eval/commit/936731b2f799f450070fb7d935ebb2b0b3dcf93e))

- **evaluation**: 4/6 — Bedrock judge retry + JudgeInfrastructureError escalation
  ([#483](https://github.com/UiPath/coder_eval/pull/483),
  [`6d2564f`](https://github.com/UiPath/coder_eval/commit/6d2564ff5bbf255a3c8f4c533572dc3058b5cb9c))

- **lint**: 2/6 — CE024 discriminated-union rule + TemplateSource wrap
  ([#483](https://github.com/UiPath/coder_eval/pull/483),
  [`6d2564f`](https://github.com/UiPath/coder_eval/commit/6d2564ff5bbf255a3c8f4c533572dc3058b5cb9c))

- **models**: 1/6 — discriminated SuccessCriterion union + fail-loud validate_registry
  ([#483](https://github.com/UiPath/coder_eval/pull/483),
  [`6d2564f`](https://github.com/UiPath/coder_eval/commit/6d2564ff5bbf255a3c8f4c533572dc3058b5cb9c))

- **models**: 3/6 — extra=forbid on mutation models + CE009 scope extension
  ([#483](https://github.com/UiPath/coder_eval/pull/483),
  [`6d2564f`](https://github.com/UiPath/coder_eval/commit/6d2564ff5bbf255a3c8f4c533572dc3058b5cb9c))

- **samples**: Add vendored SkillsBench sample tasks
  ([#473](https://github.com/UiPath/coder_eval/pull/473),
  [`6bc706f`](https://github.com/UiPath/coder_eval/commit/6bc706fd3249a16de20bbe52728c10d8ecd2b868))

- **samples**: Swap pddl-tpp-planning for court-form-filling
  ([#473](https://github.com/UiPath/coder_eval/pull/473),
  [`6bc706f`](https://github.com/UiPath/coder_eval/commit/6bc706fd3249a16de20bbe52728c10d8ecd2b868))

### Refactoring

- **evalboard**: Address PR review — consistent replicate rollup
  ([#474](https://github.com/UiPath/coder_eval/pull/474),
  [`caa9cee`](https://github.com/UiPath/coder_eval/commit/caa9ceeacc59b362b3e7b4dae995bf751d3cbcde))

- **tasks**: Group agent feature-tests under tasks/agents/ + add index
  ([#473](https://github.com/UiPath/coder_eval/pull/473),
  [`6bc706f`](https://github.com/UiPath/coder_eval/commit/6bc706fd3249a16de20bbe52728c10d8ecd2b868))

### Testing

- **agents**: Drop redundant module import in Antigravity timeout test
  ([#461](https://github.com/UiPath/coder_eval/pull/461),
  [`74e4774`](https://github.com/UiPath/coder_eval/commit/74e4774c52a7e3b2d0e91413d03379ca6ff3b81b))

- **codex**: Cover env_path_prepend PATH shadowing in _build_codex_env and start()
  ([#480](https://github.com/UiPath/coder_eval/pull/480),
  [`8a53bbe`](https://github.com/UiPath/coder_eval/commit/8a53bbe024fb628d4fd16c88b4564d9c1ba09562))

- **config**: Isolate defaults test from ambient TELEMETRY_ENABLED
  ([#483](https://github.com/UiPath/coder_eval/pull/483),
  [`6d2564f`](https://github.com/UiPath/coder_eval/commit/6d2564ff5bbf255a3c8f4c533572dc3058b5cb9c))

- **docker**: Drift guards + harden runtime kit (review feedback)
  ([#466](https://github.com/UiPath/coder_eval/pull/466),
  [`15712a6`](https://github.com/UiPath/coder_eval/commit/15712a6c74cb3a12032e5ac9035dff1f0bcb552d))


## v0.8.0 (2026-07-01)

### Bug Fixes

- **ci**: Disable telemetry workflow-wide in pr-checks (baked-in default would emit)
  ([#456](https://github.com/UiPath/coder_eval/pull/456),
  [`fe27c5f`](https://github.com/UiPath/coder_eval/commit/fe27c5fb3d198b2e626a54d8d6b330cc248cc170))

- **codex**: Fall back to full-access sandbox under the docker driver
  ([#459](https://github.com/UiPath/coder_eval/pull/459),
  [`371ab28`](https://github.com/UiPath/coder_eval/commit/371ab285b3526e950a0d9792eacec14fae8535cd))

- **codex**: Run end-to-end under the docker driver
  ([#459](https://github.com/UiPath/coder_eval/pull/459),
  [`371ab28`](https://github.com/UiPath/coder_eval/commit/371ab285b3526e950a0d9792eacec14fae8535cd))

- **deps**: Bump python-socketio/engineio to clear pip-audit CVEs
  ([#456](https://github.com/UiPath/coder_eval/pull/456),
  [`fe27c5f`](https://github.com/UiPath/coder_eval/commit/fe27c5fb3d198b2e626a54d8d6b330cc248cc170))

- **docker**: Copy ~/.claude symlinks verbatim so a plugin-cache loop can't abort setup
  ([#460](https://github.com/UiPath/coder_eval/pull/460),
  [`b192ca2`](https://github.com/UiPath/coder_eval/commit/b192ca28ad6c618a6d28b1e10eadd6bca54430c8))

- **enums**: Make FinalStatus.category exhaustive (no silent "failed" fall-through)
  ([#456](https://github.com/UiPath/coder_eval/pull/456),
  [`fe27c5f`](https://github.com/UiPath/coder_eval/commit/fe27c5fb3d198b2e626a54d8d6b330cc248cc170))

- **evalboard**: Align run-view count + trends with replicate collapse
  ([#458](https://github.com/UiPath/coder_eval/pull/458),
  [`a9f93e5`](https://github.com/UiPath/coder_eval/commit/a9f93e51b7ff2b1c21f1a892c49337306ae8cade))

- **pricing**: Inline proxy-shim deprecation message to satisfy pyright
  ([#463](https://github.com/UiPath/coder_eval/pull/463),
  [`75a07f9`](https://github.com/UiPath/coder_eval/commit/75a07f903a212269e5a608ba74833f5ab05d5e0c))

- **pricing**: Keep coder_eval.proxy.pricing as a deprecated alias
  ([#463](https://github.com/UiPath/coder_eval/pull/463),
  [`75a07f9`](https://github.com/UiPath/coder_eval/commit/75a07f903a212269e5a608ba74833f5ab05d5e0c))

- **sandbox**: Root Windows tempdir sandboxes off the user temp tree
  ([#465](https://github.com/UiPath/coder_eval/pull/465),
  [`f857c64`](https://github.com/UiPath/coder_eval/commit/f857c64d27ec861d6dafccc33406cac87fed2130))

- **telemetry**: First-run disclosure notice, README, caller-settable Source, SchemaVersion
  ([#456](https://github.com/UiPath/coder_eval/pull/456),
  [`fe27c5f`](https://github.com/UiPath/coder_eval/commit/fe27c5fb3d198b2e626a54d8d6b330cc248cc170))

- **telemetry**: Keep docker container silent to avoid double-counted Task.End
  ([#456](https://github.com/UiPath/coder_eval/pull/456),
  [`fe27c5f`](https://github.com/UiPath/coder_eval/commit/fe27c5fb3d198b2e626a54d8d6b330cc248cc170))

- **telemetry**: Never emit real telemetry from the test suite
  ([#456](https://github.com/UiPath/coder_eval/pull/456),
  [`fe27c5f`](https://github.com/UiPath/coder_eval/commit/fe27c5fb3d198b2e626a54d8d6b330cc248cc170))

- **telemetry**: One CoderEval.Task.End event per task + Category dim (drop divergent buckets)
  ([#456](https://github.com/UiPath/coder_eval/pull/456),
  [`fe27c5f`](https://github.com/UiPath/coder_eval/commit/fe27c5fb3d198b2e626a54d8d6b330cc248cc170))

- **telemetry**: Single Task.End event + Category dim, test isolation, exhaustiveness, baked-in
  default connection string ([#456](https://github.com/UiPath/coder_eval/pull/456),
  [`fe27c5f`](https://github.com/UiPath/coder_eval/commit/fe27c5fb3d198b2e626a54d8d6b330cc248cc170))

### Build System

- **docker**: Bake the codex agent into the default image
  ([#459](https://github.com/UiPath/coder_eval/pull/459),
  [`371ab28`](https://github.com/UiPath/coder_eval/commit/371ab285b3526e950a0d9792eacec14fae8535cd))

### Chores

- Bump socketio/engineio (CVE fixes) + address review feedback
  ([#462](https://github.com/UiPath/coder_eval/pull/462),
  [`80991c8`](https://github.com/UiPath/coder_eval/commit/80991c832dea9863e7845387b76a7cd6b2e2aeb6))

- Delete coder_eval.proxy.pricing shim + finish gateway-identifier cleanup
  ([#467](https://github.com/UiPath/coder_eval/pull/467),
  [`32e4e63`](https://github.com/UiPath/coder_eval/commit/32e4e638b4cffd5dc412fb7c73483b9096db29dc))

- Finish LLMGW residual cleanup left by the proxy removal
  ([#467](https://github.com/UiPath/coder_eval/pull/467),
  [`32e4e63`](https://github.com/UiPath/coder_eval/commit/32e4e638b4cffd5dc412fb7c73483b9096db29dc))

- Finish LLMGW residual cleanup left by the proxy removal (#463)
  ([#467](https://github.com/UiPath/coder_eval/pull/467),
  [`32e4e63`](https://github.com/UiPath/coder_eval/commit/32e4e638b4cffd5dc412fb7c73483b9096db29dc))

### Code Style

- Fix ruff E501 and invalid noqa warning ([#465](https://github.com/UiPath/coder_eval/pull/465),
  [`f857c64`](https://github.com/UiPath/coder_eval/commit/f857c64d27ec861d6dafccc33406cac87fed2130))

- **sandbox**: Wrap long mkdtemp dir= line to satisfy formatter
  ([#465](https://github.com/UiPath/coder_eval/pull/465),
  [`f857c64`](https://github.com/UiPath/coder_eval/commit/f857c64d27ec861d6dafccc33406cac87fed2130))

### Documentation

- Keep proxy design docs as historical records
  ([#463](https://github.com/UiPath/coder_eval/pull/463),
  [`75a07f9`](https://github.com/UiPath/coder_eval/commit/75a07f903a212269e5a608ba74833f5ab05d5e0c))

- **telemetry**: Drop remaining stale Task.End/.Failed references
  ([#456](https://github.com/UiPath/coder_eval/pull/456),
  [`fe27c5f`](https://github.com/UiPath/coder_eval/commit/fe27c5fb3d198b2e626a54d8d6b330cc248cc170))

### Features

- BUILD_FAILED status + capture build log for failed image builds
  ([#462](https://github.com/UiPath/coder_eval/pull/462),
  [`80991c8`](https://github.com/UiPath/coder_eval/commit/80991c832dea9863e7845387b76a7cd6b2e2aeb6))

- **evalboard**: Per-replicate task detail with a sticky run selector
  ([#458](https://github.com/UiPath/coder_eval/pull/458),
  [`a9f93e5`](https://github.com/UiPath/coder_eval/commit/a9f93e51b7ff2b1c21f1a892c49337306ae8cade))

- **telemetry**: Bake in a default App Insights connection string (env overrides)
  ([#456](https://github.com/UiPath/coder_eval/pull/456),
  [`fe27c5f`](https://github.com/UiPath/coder_eval/commit/fe27c5fb3d198b2e626a54d8d6b330cc248cc170))

### Refactoring

- Remove the LLM Gateway proxy backend and command
  ([#463](https://github.com/UiPath/coder_eval/pull/463),
  [`75a07f9`](https://github.com/UiPath/coder_eval/commit/75a07f903a212269e5a608ba74833f5ab05d5e0c))


## v0.7.2 (2026-06-25)

### Bug Fixes

- Address PR #450 review — CE020→CE021 rename, else-clause guard, round-trip + ctor-leak tests
  ([#450](https://github.com/UiPath/coder_eval/pull/450),
  [`faa91bd`](https://github.com/UiPath/coder_eval/commit/faa91bd579755c5593fac5e869f3846a28395cf8))

- Address PR #451 review findings (CodeQL + type/diagnostic nits)
  ([#451](https://github.com/UiPath/coder_eval/pull/451),
  [`5c285d6`](https://github.com/UiPath/coder_eval/commit/5c285d6a5749af4bc0038cfafc7bdb0bd0f1b090))

- Address PR #451 round-2 review findings (CodeQL + Docker test gap)
  ([#451](https://github.com/UiPath/coder_eval/pull/451),
  [`5c285d6`](https://github.com/UiPath/coder_eval/commit/5c285d6a5749af4bc0038cfafc7bdb0bd0f1b090))

- Code review fixes for decouple-base-agent-config-sdk-types
  ([#452](https://github.com/UiPath/coder_eval/pull/452),
  [`6283346`](https://github.com/UiPath/coder_eval/commit/62833466abef4852ebce6de8259b93f44ddd73c4))

- Code review fixes for malformed-task-json/sim-leak plan
  ([#450](https://github.com/UiPath/coder_eval/pull/450),
  [`faa91bd`](https://github.com/UiPath/coder_eval/commit/faa91bd579755c5593fac5e869f3846a28395cf8))

- Degrade-not-crash on malformed task.json + simulator scratch-dir leak
  ([#450](https://github.com/UiPath/coder_eval/pull/450),
  [`faa91bd`](https://github.com/UiPath/coder_eval/commit/faa91bd579755c5593fac5e869f3846a28395cf8))

- Full code review fixes for harness-lint-improvements
  ([#440](https://github.com/UiPath/coder_eval/pull/440),
  [`6dd7fae`](https://github.com/UiPath/coder_eval/commit/6dd7fae93880faee02d447eef7275d6f2feec44e))

- Harden CE019 scoping per final code review ([#451](https://github.com/UiPath/coder_eval/pull/451),
  [`5c285d6`](https://github.com/UiPath/coder_eval/commit/5c285d6a5749af4bc0038cfafc7bdb0bd0f1b090))

- High-priority code-review fixes (review 260622-1009)
  ([#439](https://github.com/UiPath/coder_eval/pull/439),
  [`3e2d668`](https://github.com/UiPath/coder_eval/commit/3e2d668fc2f8c5df79d301568873ba745981b167))

- PR-gate + review fixes (merge main, finalize hardening)
  ([#440](https://github.com/UiPath/coder_eval/pull/440),
  [`6dd7fae`](https://github.com/UiPath/coder_eval/commit/6dd7fae93880faee02d447eef7275d6f2feec44e))

- **cli**: Drop codex+backend rejection — --backend routes the judges too
  ([#440](https://github.com/UiPath/coder_eval/pull/440),
  [`6dd7fae`](https://github.com/UiPath/coder_eval/commit/6dd7fae93880faee02d447eef7275d6f2feec44e))

- **docker**: 1/3 — degrade-not-crash on malformed task.json
  ([#450](https://github.com/UiPath/coder_eval/pull/450),
  [`faa91bd`](https://github.com/UiPath/coder_eval/commit/faa91bd579755c5593fac5e869f3846a28395cf8))

- **evalboard**: Don't let mature-source scan crash the run page
  ([#455](https://github.com/UiPath/coder_eval/pull/455),
  [`b5d838d`](https://github.com/UiPath/coder_eval/commit/b5d838d679dc83a739933d7d0c96d042289c5996))

- **lint**: Full review fix — CE018 also flags list/set membership denylists
  ([#440](https://github.com/UiPath/coder_eval/pull/440),
  [`6dd7fae`](https://github.com/UiPath/coder_eval/commit/6dd7fae93880faee02d447eef7275d6f2feec44e))

- **models**: Correct misleading type:none validation suggestion
  ([#440](https://github.com/UiPath/coder_eval/pull/440),
  [`6dd7fae`](https://github.com/UiPath/coder_eval/commit/6dd7fae93880faee02d447eef7275d6f2feec44e))

- **orchestrator**: Code review fixes for phase 1
  ([#439](https://github.com/UiPath/coder_eval/pull/439),
  [`3e2d668`](https://github.com/UiPath/coder_eval/commit/3e2d668fc2f8c5df79d301568873ba745981b167))

- **reports**: Avoid implicit string concat in denominator render line
  ([#453](https://github.com/UiPath/coder_eval/pull/453),
  [`c2412a7`](https://github.com/UiPath/coder_eval/commit/c2412a78281ed3c543074e278d7baa8a3ce93380))

- **reports**: Re-evaluate thresholds on missing-aggregator path so completion_rate is consistent
  ([#453](https://github.com/UiPath/coder_eval/pull/453),
  [`c2412a7`](https://github.com/UiPath/coder_eval/commit/c2412a78281ed3c543074e278d7baa8a3ce93380))

- **reports-html**: Phase 3 — _status_badge dispatches on FinalStatus.category
  ([#439](https://github.com/UiPath/coder_eval/pull/439),
  [`3e2d668`](https://github.com/UiPath/coder_eval/commit/3e2d668fc2f8c5df79d301568873ba745981b167))

- **results**: Phase 1 — calculate_weighted_score fails loud on length mismatch
  ([#439](https://github.com/UiPath/coder_eval/pull/439),
  [`3e2d668`](https://github.com/UiPath/coder_eval/commit/3e2d668fc2f8c5df79d301568873ba745981b167))

- **sampling**: Keep --sample-per-stratum nondeterministic by default
  ([#439](https://github.com/UiPath/coder_eval/pull/439),
  [`3e2d668`](https://github.com/UiPath/coder_eval/commit/3e2d668fc2f8c5df79d301568873ba745981b167))

- **scoring**: Phase 7 — fail-loud weighted score + single-sourced all_criteria_passed gate
  ([#440](https://github.com/UiPath/coder_eval/pull/440),
  [`6dd7fae`](https://github.com/UiPath/coder_eval/commit/6dd7fae93880faee02d447eef7275d6f2feec44e))

- **simulation**: 2/3 — self-cleaning UserSimulator.start() (no scratch leak)
  ([#450](https://github.com/UiPath/coder_eval/pull/450),
  [`faa91bd`](https://github.com/UiPath/coder_eval/commit/faa91bd579755c5593fac5e869f3846a28395cf8))

- **task-loader**: Phase 2 — CLI --sample-per-stratum reproducible-by-default
  ([#439](https://github.com/UiPath/coder_eval/pull/439),
  [`3e2d668`](https://github.com/UiPath/coder_eval/commit/3e2d668fc2f8c5df79d301568873ba745981b167))

- **telemetry**: Code review fixes for phase 1
  ([#441](https://github.com/UiPath/coder_eval/pull/441),
  [`2f84789`](https://github.com/UiPath/coder_eval/commit/2f847893c87b283b2720b7b2de60e5deb08433d0))

- **telemetry**: Code review fixes for phase 4
  ([#441](https://github.com/UiPath/coder_eval/pull/441),
  [`2f84789`](https://github.com/UiPath/coder_eval/commit/2f847893c87b283b2720b7b2de60e5deb08433d0))

- **telemetry**: Full code review fixes — docker per-task events
  ([#441](https://github.com/UiPath/coder_eval/pull/441),
  [`2f84789`](https://github.com/UiPath/coder_eval/commit/2f847893c87b283b2720b7b2de60e5deb08433d0))

- **telemetry**: Review fixes + reshape to generic product telemetry
  ([#441](https://github.com/UiPath/coder_eval/pull/441),
  [`2f84789`](https://github.com/UiPath/coder_eval/commit/2f847893c87b283b2720b7b2de60e5deb08433d0))

- **tests**: Reset deadline-break scenario state between replays
  ([#451](https://github.com/UiPath/coder_eval/pull/451),
  [`5c285d6`](https://github.com/UiPath/coder_eval/commit/5c285d6a5749af4bc0038cfafc7bdb0bd0f1b090))

- **types**: Phase 1 — promote pyright string-concat + missing-type-arg to error
  ([#440](https://github.com/UiPath/coder_eval/pull/440),
  [`6dd7fae`](https://github.com/UiPath/coder_eval/commit/6dd7fae93880faee02d447eef7275d6f2feec44e))

### Chores

- Harness & lint improvements (code-review 2026-06-22)
  ([#440](https://github.com/UiPath/coder_eval/pull/440),
  [`6dd7fae`](https://github.com/UiPath/coder_eval/commit/6dd7fae93880faee02d447eef7275d6f2feec44e))

- Remove suite-aggregate-error-row-denominator scratch docs from repo root
  ([#453](https://github.com/UiPath/coder_eval/pull/453),
  [`c2412a7`](https://github.com/UiPath/coder_eval/commit/c2412a78281ed3c543074e278d7baa8a3ce93380))

- **claude**: Shared axis catalog, cr-workflow scoring fix + 3-way change class
  ([#445](https://github.com/UiPath/coder_eval/pull/445),
  [`21b7386`](https://github.com/UiPath/coder_eval/commit/21b7386a74938d6ca51087caec218f7791313dba))

- **evalboard**: Gitignore the coverage/ output dir
  ([#439](https://github.com/UiPath/coder_eval/pull/439),
  [`3e2d668`](https://github.com/UiPath/coder_eval/commit/3e2d668fc2f8c5df79d301568873ba745981b167))

- **evalboard**: Remove deploy plumbing (moving to coder_eval_uipath)
  ([#447](https://github.com/UiPath/coder_eval/pull/447),
  [`50f570a`](https://github.com/UiPath/coder_eval/commit/50f570a11e6d22dc43e3925516cf55f99a18c662))

- **lint**: Phase 5 — enable PLR0915/PLR0912 ceiling with tracked debt markers
  ([#440](https://github.com/UiPath/coder_eval/pull/440),
  [`6dd7fae`](https://github.com/UiPath/coder_eval/commit/6dd7fae93880faee02d447eef7275d6f2feec44e))

- **lint**: Renumber dialog-loop statement-cap rule CE019 -> CE020
  ([#451](https://github.com/UiPath/coder_eval/pull/451),
  [`5c285d6`](https://github.com/UiPath/coder_eval/commit/5c285d6a5749af4bc0038cfafc7bdb0bd0f1b090))

### Code Style

- **test**: Drop extra blank line left by finalize-test removal
  ([#440](https://github.com/UiPath/coder_eval/pull/440),
  [`6dd7fae`](https://github.com/UiPath/coder_eval/commit/6dd7fae93880faee02d447eef7275d6f2feec44e))

### Continuous Integration

- **claude-review**: Drop dead cross-repo skills-YAML check from prompt
  ([#444](https://github.com/UiPath/coder_eval/pull/444),
  [`6c4bc35`](https://github.com/UiPath/coder_eval/commit/6c4bc351b9f1edf419b8bb53567edb2a526e19dd))

- **claude-review**: Harden PR-review workflow for public open-sourcing
  ([#444](https://github.com/UiPath/coder_eval/pull/444),
  [`6c4bc35`](https://github.com/UiPath/coder_eval/commit/6c4bc351b9f1edf419b8bb53567edb2a526e19dd))

- **claude-review**: Harden PR-review workflow for public repo
  ([#444](https://github.com/UiPath/coder_eval/pull/444),
  [`6c4bc35`](https://github.com/UiPath/coder_eval/commit/6c4bc351b9f1edf419b8bb53567edb2a526e19dd))

### Documentation

- Mark suite-aggregate-error-row-denominator plan complete
  ([#453](https://github.com/UiPath/coder_eval/pull/453),
  [`c2412a7`](https://github.com/UiPath/coder_eval/commit/c2412a78281ed3c543074e278d7baa8a3ce93380))

- **evalboard**: Make public README local-only, drop deploy/Azure details
  ([#447](https://github.com/UiPath/coder_eval/pull/447),
  [`50f570a`](https://github.com/UiPath/coder_eval/commit/50f570a11e6d22dc43e3925516cf55f99a18c662))

- **evalboard**: Repoint README deploy section after plumbing move
  ([#447](https://github.com/UiPath/coder_eval/pull/447),
  [`50f570a`](https://github.com/UiPath/coder_eval/commit/50f570a11e6d22dc43e3925516cf55f99a18c662))

- **orchestrator**: Full code review fix — clarify finalize score-wrap asymmetry
  ([#439](https://github.com/UiPath/coder_eval/pull/439),
  [`3e2d668`](https://github.com/UiPath/coder_eval/commit/3e2d668fc2f8c5df79d301568873ba745981b167))

### Features

- **cli**: Phase 12 — reject codex+bedrock/proxy, document sampling reproducibility
  ([#440](https://github.com/UiPath/coder_eval/pull/440),
  [`6dd7fae`](https://github.com/UiPath/coder_eval/commit/6dd7fae93880faee02d447eef7275d6f2feec44e))

- **cli**: Phase 6 — constrain proxy --vendor with click.Choice
  ([#440](https://github.com/UiPath/coder_eval/pull/440),
  [`6dd7fae`](https://github.com/UiPath/coder_eval/commit/6dd7fae93880faee02d447eef7275d6f2feec44e))

- **docker**: Mount a lean RW copy of ~/.claude instead of host dir read-only
  ([#446](https://github.com/UiPath/coder_eval/pull/446),
  [`dfcbf5b`](https://github.com/UiPath/coder_eval/commit/dfcbf5b0e12652e7a536d3d328fee61ddf8494bb))

- **evalboard**: Add EVALBOARD_EDITION edition flag
  ([#447](https://github.com/UiPath/coder_eval/pull/447),
  [`50f570a`](https://github.com/UiPath/coder_eval/commit/50f570a11e6d22dc43e3925516cf55f99a18c662))

- **evalboard**: Add OSS edition and move deploy plumbing to coder_eval_uipath
  ([#447](https://github.com/UiPath/coder_eval/pull/447),
  [`50f570a`](https://github.com/UiPath/coder_eval/commit/50f570a11e6d22dc43e3925516cf55f99a18c662))

- **evalboard**: Clickable mature test links + trends maturity
  ([#455](https://github.com/UiPath/coder_eval/pull/455),
  [`b5d838d`](https://github.com/UiPath/coder_eval/commit/b5d838d679dc83a739933d7d0c96d042289c5996))

- **evalboard**: Gate more internal-only surfaces in OSS edition
  ([#447](https://github.com/UiPath/coder_eval/pull/447),
  [`50f570a`](https://github.com/UiPath/coder_eval/commit/50f570a11e6d22dc43e3925516cf55f99a18c662))

- **evalboard**: Hide internal-only nav links in OSS edition
  ([#447](https://github.com/UiPath/coder_eval/pull/447),
  [`50f570a`](https://github.com/UiPath/coder_eval/commit/50f570a11e6d22dc43e3925516cf55f99a18c662))

- **evalboard**: Mature-link popover + simpler trends maturity
  ([#455](https://github.com/UiPath/coder_eval/pull/455),
  [`b5d838d`](https://github.com/UiPath/coder_eval/commit/b5d838d679dc83a739933d7d0c96d042289c5996))

- **lint**: Phase 3 — CE018 no-final-status-name-denylist + dispatch _status_badge on category
  ([#440](https://github.com/UiPath/coder_eval/pull/440),
  [`6dd7fae`](https://github.com/UiPath/coder_eval/commit/6dd7fae93880faee02d447eef7275d6f2feec44e))

- **reports**: 1/2 — record ERROR-row exclusion + gateable completion_rate in suite aggregates
  ([#453](https://github.com/UiPath/coder_eval/pull/453),
  [`c2412a7`](https://github.com/UiPath/coder_eval/commit/c2412a78281ed3c543074e278d7baa8a3ce93380))

- **reports**: Record ERROR-row exclusion & expose gateable completion_rate in suite aggregates
  ([#453](https://github.com/UiPath/coder_eval/pull/453),
  [`c2412a7`](https://github.com/UiPath/coder_eval/commit/c2412a78281ed3c543074e278d7baa8a3ce93380))

- **telemetry**: Opt-out usage telemetry via OpenTelemetry → Azure App Insights customEvents
  ([#441](https://github.com/UiPath/coder_eval/pull/441),
  [`2f84789`](https://github.com/UiPath/coder_eval/commit/2f847893c87b283b2720b7b2de60e5deb08433d0))

- **telemetry**: Phase 1 — telemetry module + config + core deps
  ([#441](https://github.com/UiPath/coder_eval/pull/441),
  [`2f84789`](https://github.com/UiPath/coder_eval/commit/2f847893c87b283b2720b7b2de60e5deb08433d0))

- **telemetry**: Phase 2 — emit run-start + task-end/failed events
  ([#441](https://github.com/UiPath/coder_eval/pull/441),
  [`2f84789`](https://github.com/UiPath/coder_eval/commit/2f847893c87b283b2720b7b2de60e5deb08433d0))

- **telemetry**: Phase 3 — per-command CoderEval.Cli.<name> events
  ([#441](https://github.com/UiPath/coder_eval/pull/441),
  [`2f84789`](https://github.com/UiPath/coder_eval/commit/2f847893c87b283b2720b7b2de60e5deb08433d0))

- **telemetry**: Phase 4 — CE018 lint rule + feature doc
  ([#441](https://github.com/UiPath/coder_eval/pull/441),
  [`2f84789`](https://github.com/UiPath/coder_eval/commit/2f847893c87b283b2720b7b2de60e5deb08433d0))

### Refactoring

- Decompose agent turn-loops + the five remaining god-functions
  ([#451](https://github.com/UiPath/coder_eval/pull/451),
  [`5c285d6`](https://github.com/UiPath/coder_eval/commit/5c285d6a5749af4bc0038cfafc7bdb0bd0f1b090))

- **agents**: Phase 4 — shared finalize/raise/partial-record kernels on Agent
  ([#451](https://github.com/UiPath/coder_eval/pull/451),
  [`5c285d6`](https://github.com/UiPath/coder_eval/commit/5c285d6a5749af4bc0038cfafc7bdb0bd0f1b090))

- **claude-agent**: Phase 2 — extract _ClaudeTurnState + _build_claude_query
  ([#451](https://github.com/UiPath/coder_eval/pull/451),
  [`5c285d6`](https://github.com/UiPath/coder_eval/commit/5c285d6a5749af4bc0038cfafc7bdb0bd0f1b090))

- **cli**: Phase 9 — extract pure heartbeat_is_fresh watchdog predicate
  ([#440](https://github.com/UiPath/coder_eval/pull/440),
  [`6dd7fae`](https://github.com/UiPath/coder_eval/commit/6dd7fae93880faee02d447eef7275d6f2feec44e))

- **codex-agent**: Drop dead prev_prompt_tokens + fix post-watchdog timeout state
  ([#451](https://github.com/UiPath/coder_eval/pull/451),
  [`5c285d6`](https://github.com/UiPath/coder_eval/commit/5c285d6a5749af4bc0038cfafc7bdb0bd0f1b090))

- **codex-agent**: Phase 3 — extract _CodexTurnState
  ([#451](https://github.com/UiPath/coder_eval/pull/451),
  [`5c285d6`](https://github.com/UiPath/coder_eval/commit/5c285d6a5749af4bc0038cfafc7bdb0bd0f1b090))

- **docker**: 4/5 — decompose DockerRunner.run into staged helpers
  ([#451](https://github.com/UiPath/coder_eval/pull/451),
  [`5c285d6`](https://github.com/UiPath/coder_eval/commit/5c285d6a5749af4bc0038cfafc7bdb0bd0f1b090))

- **errors**: 1/5 — decompose categorize_error into group classifiers
  ([#451](https://github.com/UiPath/coder_eval/pull/451),
  [`5c285d6`](https://github.com/UiPath/coder_eval/commit/5c285d6a5749af4bc0038cfafc7bdb0bd0f1b090))

- **errors**: Phase 8 — delete dead ErrorCategory members + add liveness contract test
  ([#440](https://github.com/UiPath/coder_eval/pull/440),
  [`6dd7fae`](https://github.com/UiPath/coder_eval/commit/6dd7fae93880faee02d447eef7275d6f2feec44e))

- **models**: 1/2 — decouple BaseAgentConfig from claude-code-sdk types
  ([#452](https://github.com/UiPath/coder_eval/pull/452),
  [`6283346`](https://github.com/UiPath/coder_eval/commit/62833466abef4852ebce6de8259b93f44ddd73c4))

- **models**: Decouple BaseAgentConfig from claude-code-sdk types (+ CE020 guard)
  ([#452](https://github.com/UiPath/coder_eval/pull/452),
  [`6283346`](https://github.com/UiPath/coder_eval/commit/62833466abef4852ebce6de8259b93f44ddd73c4))

- **models**: Phase 4 — declare type on BaseSuccessCriterion, drop getattr type-holes
  ([#440](https://github.com/UiPath/coder_eval/pull/440),
  [`6dd7fae`](https://github.com/UiPath/coder_eval/commit/6dd7fae93880faee02d447eef7275d6f2feec44e))

- **orchestrator**: 5/5 — decompose _simulation_dialog_loop + add CE019 ratchet
  ([#451](https://github.com/UiPath/coder_eval/pull/451),
  [`5c285d6`](https://github.com/UiPath/coder_eval/commit/5c285d6a5749af4bc0038cfafc7bdb0bd0f1b090))

- **orchestrator**: Phase 2 — drop run_batch wrapper, promote reportImportCycles to error
  ([#440](https://github.com/UiPath/coder_eval/pull/440),
  [`6dd7fae`](https://github.com/UiPath/coder_eval/commit/6dd7fae93880faee02d447eef7275d6f2feec44e))

- **orchestrator**: Phase 6 — DRY simulation telemetry via one builder
  ([#439](https://github.com/UiPath/coder_eval/pull/439),
  [`3e2d668`](https://github.com/UiPath/coder_eval/commit/3e2d668fc2f8c5df79d301568873ba745981b167))

- **reports**: 2/5 — decompose generate_markdown into section helpers
  ([#451](https://github.com/UiPath/coder_eval/pull/451),
  [`5c285d6`](https://github.com/UiPath/coder_eval/commit/5c285d6a5749af4bc0038cfafc7bdb0bd0f1b090))

- **reports**: 3/5 — decompose generate_experiment_report into section helpers
  ([#451](https://github.com/UiPath/coder_eval/pull/451),
  [`5c285d6`](https://github.com/UiPath/coder_eval/commit/5c285d6a5749af4bc0038cfafc7bdb0bd0f1b090))

### Testing

- Address review — guard SettingSource mirror + freeze codex task.json shape
  ([#452](https://github.com/UiPath/coder_eval/pull/452),
  [`6283346`](https://github.com/UiPath/coder_eval/commit/62833466abef4852ebce6de8259b93f44ddd73c4))

- Move setting_sources merge-strategy assertion to Claude subclass
  ([#452](https://github.com/UiPath/coder_eval/pull/452),
  [`6283346`](https://github.com/UiPath/coder_eval/commit/62833466abef4852ebce6de8259b93f44ddd73c4))

- Resolve CodeQL mixed-import alert on agent_config
  ([#452](https://github.com/UiPath/coder_eval/pull/452),
  [`6283346`](https://github.com/UiPath/coder_eval/commit/62833466abef4852ebce6de8259b93f44ddd73c4))

- Stop codex CLI test leaking settings.api_backend into later tests
  ([#440](https://github.com/UiPath/coder_eval/pull/440),
  [`6dd7fae`](https://github.com/UiPath/coder_eval/commit/6dd7fae93880faee02d447eef7275d6f2feec44e))

- **agents**: Phase 1 review fix — keep Claude goldens collectable without codex
  ([#451](https://github.com/UiPath/coder_eval/pull/451),
  [`5c285d6`](https://github.com/UiPath/coder_eval/commit/5c285d6a5749af4bc0038cfafc7bdb0bd0f1b090))

- **agents**: Phase 1 — golden-master characterization harness
  ([#451](https://github.com/UiPath/coder_eval/pull/451),
  [`5c285d6`](https://github.com/UiPath/coder_eval/commit/5c285d6a5749af4bc0038cfafc7bdb0bd0f1b090))

- **agents**: Phase 4 review fix — lock _state-before-finalize ordering
  ([#451](https://github.com/UiPath/coder_eval/pull/451),
  [`5c285d6`](https://github.com/UiPath/coder_eval/commit/5c285d6a5749af4bc0038cfafc7bdb0bd0f1b090))

- **cli**: Code review fixes for phase 4 ([#439](https://github.com/UiPath/coder_eval/pull/439),
  [`3e2d668`](https://github.com/UiPath/coder_eval/commit/3e2d668fc2f8c5df79d301568873ba745981b167))

- **cli**: Phase 11 — CliRunner coverage for the report command
  ([#440](https://github.com/UiPath/coder_eval/pull/440),
  [`6dd7fae`](https://github.com/UiPath/coder_eval/commit/6dd7fae93880faee02d447eef7275d6f2feec44e))

- **cli**: Phase 4 — report CLI tests + extract heartbeat_is_alive helper
  ([#439](https://github.com/UiPath/coder_eval/pull/439),
  [`3e2d668`](https://github.com/UiPath/coder_eval/commit/3e2d668fc2f8c5df79d301568873ba745981b167))

- **codex**: Split SDK-independent unit tests into an ungated file
  ([#451](https://github.com/UiPath/coder_eval/pull/451),
  [`5c285d6`](https://github.com/UiPath/coder_eval/commit/5c285d6a5749af4bc0038cfafc7bdb0bd0f1b090))

- **evalboard**: Cover EVALBOARD_EDITION gate
  ([#447](https://github.com/UiPath/coder_eval/pull/447),
  [`50f570a`](https://github.com/UiPath/coder_eval/commit/50f570a11e6d22dc43e3925516cf55f99a18c662))

- **lint**: 2/2 — add CE020 banning SDK-typed BaseAgentConfig fields
  ([#452](https://github.com/UiPath/coder_eval/pull/452),
  [`6283346`](https://github.com/UiPath/coder_eval/commit/62833466abef4852ebce6de8259b93f44ddd73c4))

- **lint**: 3/3 — add CE020 guarding EvaluationResult.model_validate_json
  ([#450](https://github.com/UiPath/coder_eval/pull/450),
  [`faa91bd`](https://github.com/UiPath/coder_eval/commit/faa91bd579755c5593fac5e869f3846a28395cf8))

- **lint**: Code review fix for phase 3 — pin CE018 denylist to FinalStatus
  ([#440](https://github.com/UiPath/coder_eval/pull/440),
  [`6dd7fae`](https://github.com/UiPath/coder_eval/commit/6dd7fae93880faee02d447eef7275d6f2feec44e))

- **models**: Code review fix for phase 4 — accurate union test name + bogus-tag assertion
  ([#440](https://github.com/UiPath/coder_eval/pull/440),
  [`6dd7fae`](https://github.com/UiPath/coder_eval/commit/6dd7fae93880faee02d447eef7275d6f2feec44e))

- **proxy**: Phase 10 — TokenManager._acquire_token HTTP unit test via httpx.MockTransport
  ([#440](https://github.com/UiPath/coder_eval/pull/440),
  [`6dd7fae`](https://github.com/UiPath/coder_eval/commit/6dd7fae93880faee02d447eef7275d6f2feec44e))

- **reports**: 2/2 — ERROR-row denominator + completion_rate gate tests
  ([#453](https://github.com/UiPath/coder_eval/pull/453),
  [`c2412a7`](https://github.com/UiPath/coder_eval/commit/c2412a78281ed3c543074e278d7baa8a3ce93380))

- **telemetry**: Strip ANSI before asserting on --help output
  ([#441](https://github.com/UiPath/coder_eval/pull/441),
  [`2f84789`](https://github.com/UiPath/coder_eval/commit/2f847893c87b283b2720b7b2de60e5deb08433d0))


## v0.7.1 (2026-06-22)

### Bug Fixes

- **cli**: Harden aggregate recovery path and disclose rebuild scope
  ([#438](https://github.com/UiPath/coder_eval/pull/438),
  [`5f32a13`](https://github.com/UiPath/coder_eval/commit/5f32a13003b51cdeb5be68c48ffcd5931d73a3be))

- **deps**: Bump pydantic-settings and msgpack to patch CVEs
  ([#435](https://github.com/UiPath/coder_eval/pull/435),
  [`24a0e06`](https://github.com/UiPath/coder_eval/commit/24a0e06d7d78334b062cbb47dbf51a8d7a68c6d7))

- **evalboard**: Date and sort ad-hoc runs by run start_time
  ([#432](https://github.com/UiPath/coder_eval/pull/432),
  [`f058ea8`](https://github.com/UiPath/coder_eval/commit/f058ea8a4c5e6276c8391229bcf585d084861c17))

- **evalboard**: Exclude mature-skipped tasks from run-page cost/duration metrics
  ([#427](https://github.com/UiPath/coder_eval/pull/427),
  [`18f303c`](https://github.com/UiPath/coder_eval/commit/18f303c2e67008000a7d3d91ebf39f477b67f419))

### Build System

- Sync uv.lock after dropping pylint/radon scoring deps
  ([#430](https://github.com/UiPath/coder_eval/pull/430),
  [`1abf449`](https://github.com/UiPath/coder_eval/commit/1abf449359c7b9fb03e0c12bf03f77107fdc2b50))

### Chores

- Relicense from MIT to Apache 2.0 with UiPath copyright notice
  ([#435](https://github.com/UiPath/coder_eval/pull/435),
  [`24a0e06`](https://github.com/UiPath/coder_eval/commit/24a0e06d7d78334b062cbb47dbf51a8d7a68c6d7))

### Features

- Restore HTML report generation and the report command
  ([#430](https://github.com/UiPath/coder_eval/pull/430),
  [`1abf449`](https://github.com/UiPath/coder_eval/commit/1abf449359c7b9fb03e0c12bf03f77107fdc2b50))

- **cli**: Add aggregate to rebuild run.json/run.md from task.json
  ([#438](https://github.com/UiPath/coder_eval/pull/438),
  [`5f32a13`](https://github.com/UiPath/coder_eval/commit/5f32a13003b51cdeb5be68c48ffcd5931d73a3be))

- **cli**: Add summarize to rebuild run.json/run.md from task.json
  ([#438](https://github.com/UiPath/coder_eval/pull/438),
  [`5f32a13`](https://github.com/UiPath/coder_eval/commit/5f32a13003b51cdeb5be68c48ffcd5931d73a3be))

- **evalboard**: Cap ad-hoc runs with a show-all toggle
  ([#432](https://github.com/UiPath/coder_eval/pull/432),
  [`f058ea8`](https://github.com/UiPath/coder_eval/commit/f058ea8a4c5e6276c8391229bcf585d084861c17))

- **evalboard**: Date, sort, and paginate the ad-hoc runs section
  ([#432](https://github.com/UiPath/coder_eval/pull/432),
  [`f058ea8`](https://github.com/UiPath/coder_eval/commit/f058ea8a4c5e6276c8391229bcf585d084861c17))

- **evalboard**: Mark skipped-mature tasks with a non-clickable badge
  ([#427](https://github.com/UiPath/coder_eval/pull/427),
  [`18f303c`](https://github.com/UiPath/coder_eval/commit/18f303c2e67008000a7d3d91ebf39f477b67f419))

- **evalboard**: Show mature tasks as green passes, keep trend averages honest
  ([#427](https://github.com/UiPath/coder_eval/pull/427),
  [`18f303c`](https://github.com/UiPath/coder_eval/commit/18f303c2e67008000a7d3d91ebf39f477b67f419))

### Refactoring

- Remove dormant features ahead of open-sourcing
  ([#430](https://github.com/UiPath/coder_eval/pull/430),
  [`1abf449`](https://github.com/UiPath/coder_eval/commit/1abf449359c7b9fb03e0c12bf03f77107fdc2b50))

- Remove dormant features ahead of open-sourcing (#422)
  ([#430](https://github.com/UiPath/coder_eval/pull/430),
  [`1abf449`](https://github.com/UiPath/coder_eval/commit/1abf449359c7b9fb03e0c12bf03f77107fdc2b50))

- Scrub stale references to removed features ([#430](https://github.com/UiPath/coder_eval/pull/430),
  [`1abf449`](https://github.com/UiPath/coder_eval/commit/1abf449359c7b9fb03e0c12bf03f77107fdc2b50))

- **cli**: Rename summarize command to aggregate
  ([#438](https://github.com/UiPath/coder_eval/pull/438),
  [`5f32a13`](https://github.com/UiPath/coder_eval/commit/5f32a13003b51cdeb5be68c48ffcd5931d73a3be))

### Testing

- Isolate version-info tests from the host UiPath CLI
  ([#430](https://github.com/UiPath/coder_eval/pull/430),
  [`1abf449`](https://github.com/UiPath/coder_eval/commit/1abf449359c7b9fb03e0c12bf03f77107fdc2b50))


## v0.7.0 (2026-06-16)

### Bug Fixes

- Address pr:424 review nits (pricing seam + error-category contract)
  ([#424](https://github.com/UiPath/coder_eval/pull/424),
  [`b24010e`](https://github.com/UiPath/coder_eval/commit/b24010e28f6768a62ba456de4e473ca04a3e9a85))

- Code review findings for pricing seam + error category
  ([#424](https://github.com/UiPath/coder_eval/pull/424),
  [`b24010e`](https://github.com/UiPath/coder_eval/commit/b24010e28f6768a62ba456de4e473ca04a3e9a85))

- Full code review fixes for delegate-sdk base primitives (phases 1-3)
  ([#424](https://github.com/UiPath/coder_eval/pull/424),
  [`b24010e`](https://github.com/UiPath/coder_eval/commit/b24010e28f6768a62ba456de4e473ca04a3e9a85))

- **errors**: Code review fixes for phase 1 ([#424](https://github.com/UiPath/coder_eval/pull/424),
  [`b24010e`](https://github.com/UiPath/coder_eval/commit/b24010e28f6768a62ba456de4e473ca04a3e9a85))

### Documentation

- Correct the is-not-None lookup rationale (review follow-up)
  ([#424](https://github.com/UiPath/coder_eval/pull/424),
  [`b24010e`](https://github.com/UiPath/coder_eval/commit/b24010e28f6768a62ba456de4e473ca04a3e9a85))

- Surface register_pricing extension point + BYOA worked example
  ([#424](https://github.com/UiPath/coder_eval/pull/424),
  [`b24010e`](https://github.com/UiPath/coder_eval/commit/b24010e28f6768a62ba456de4e473ca04a3e9a85))

- **pricing**: Add feature spec for the pricing registration seam
  ([#424](https://github.com/UiPath/coder_eval/pull/424),
  [`b24010e`](https://github.com/UiPath/coder_eval/commit/b24010e28f6768a62ba456de4e473ca04a3e9a85))

### Features

- Delegate-sdk base primitives — AgentConfigError + pricing registration seam
  ([#424](https://github.com/UiPath/coder_eval/pull/424),
  [`b24010e`](https://github.com/UiPath/coder_eval/commit/b24010e28f6768a62ba456de4e473ca04a3e9a85))

- **errors**: Add generic AgentConfigError + AGENT_CONFIG_ERROR category
  ([#424](https://github.com/UiPath/coder_eval/pull/424),
  [`b24010e`](https://github.com/UiPath/coder_eval/commit/b24010e28f6768a62ba456de4e473ca04a3e9a85))

- **pricing**: Add register_pricing seam for plugin-contributed model rates
  ([#424](https://github.com/UiPath/coder_eval/pull/424),
  [`b24010e`](https://github.com/UiPath/coder_eval/commit/b24010e28f6768a62ba456de4e473ca04a3e9a85))


## v0.6.2 (2026-06-16)

### Build System

- **deps**: Bump aiohttp, cryptography, python-multipart, starlette for CVEs
  ([#426](https://github.com/UiPath/coder_eval/pull/426),
  [`633628c`](https://github.com/UiPath/coder_eval/commit/633628c1fc6d0fb96987aa317c87f7b36c817ab3))

### Continuous Integration

- **release**: Drop dry-run input; dispatch always cuts a release
  ([#426](https://github.com/UiPath/coder_eval/pull/426),
  [`633628c`](https://github.com/UiPath/coder_eval/commit/633628c1fc6d0fb96987aa317c87f7b36c817ab3))

- **release**: Make releases manual via dispatch with dry-run preview
  ([#426](https://github.com/UiPath/coder_eval/pull/426),
  [`633628c`](https://github.com/UiPath/coder_eval/commit/633628c1fc6d0fb96987aa317c87f7b36c817ab3))

- **release**: Manual dispatch-triggered release + versioned agent image
  ([#426](https://github.com/UiPath/coder_eval/pull/426),
  [`633628c`](https://github.com/UiPath/coder_eval/commit/633628c1fc6d0fb96987aa317c87f7b36c817ab3))

- **release**: Pick bump level at dispatch, not from commit messages
  ([#426](https://github.com/UiPath/coder_eval/pull/426),
  [`633628c`](https://github.com/UiPath/coder_eval/commit/633628c1fc6d0fb96987aa317c87f7b36c817ab3))

- **release**: Publish versioned agent image from the release job
  ([#426](https://github.com/UiPath/coder_eval/pull/426),
  [`633628c`](https://github.com/UiPath/coder_eval/commit/633628c1fc6d0fb96987aa317c87f7b36c817ab3))


## v0.6.1 (2026-06-15)

### Bug Fixes

- **docker**: Pin Claude Code CLI version (was @latest)
  ([#425](https://github.com/UiPath/coder_eval/pull/425),
  [`ae47e87`](https://github.com/UiPath/coder_eval/commit/ae47e875274b428825b9482365df2745e132fe70))

### Continuous Integration

- Remove validate-skills-yamls job from PR checks
  ([#413](https://github.com/UiPath/coder_eval/pull/413),
  [`c205207`](https://github.com/UiPath/coder_eval/commit/c2052072fc0421b852046723749b74f6d1002fcf))

### Documentation

- **docker**: Trim historical narrative from Claude Code pin comment
  ([#425](https://github.com/UiPath/coder_eval/pull/425),
  [`ae47e87`](https://github.com/UiPath/coder_eval/commit/ae47e875274b428825b9482365df2745e132fe70))


## v0.6.0 (2026-06-15)

### Chores

- Drop leftover eval-runner config refs from coder_eval
  ([#419](https://github.com/UiPath/coder_eval/pull/419),
  [`b862794`](https://github.com/UiPath/coder_eval/commit/b8627940ee940cbddd6ccf1c5a8c9c831f7af79b))

### Continuous Integration

- Drop UiPath/skills cross-repo checks ([#419](https://github.com/UiPath/coder_eval/pull/419),
  [`b862794`](https://github.com/UiPath/coder_eval/commit/b8627940ee940cbddd6ccf1c5a8c9c831f7af79b))

### Features

- **review**: Add workflow-orchestrated code-review skill + harden the full review command
  ([#423](https://github.com/UiPath/coder_eval/pull/423),
  [`ac7b2e0`](https://github.com/UiPath/coder_eval/commit/ac7b2e01b25ca9ee5133aaf492af1358309e3587))

### Refactoring

- Move dashboard CI to coder_eval_uipath (as eval-runner)
  ([#419](https://github.com/UiPath/coder_eval/pull/419),
  [`b862794`](https://github.com/UiPath/coder_eval/commit/b8627940ee940cbddd6ccf1c5a8c9c831f7af79b))

- Remove dashboard CI (moved to coder_eval_uipath as eval-runner)
  ([#419](https://github.com/UiPath/coder_eval/pull/419),
  [`b862794`](https://github.com/UiPath/coder_eval/commit/b8627940ee940cbddd6ccf1c5a8c9c831f7af79b))


## v0.5.0 (2026-06-12)

### Bug Fixes

- **agents**: Address PR #416 review feedback
  ([#416](https://github.com/UiPath/coder_eval/pull/416),
  [`df9ac29`](https://github.com/UiPath/coder_eval/commit/df9ac2909760d1083fc42760fa2bf1ddf941e6f1))

- **agents**: Break plugins<->registry import cycle (CodeQL)
  ([#416](https://github.com/UiPath/coder_eval/pull/416),
  [`df9ac29`](https://github.com/UiPath/coder_eval/commit/df9ac2909760d1083fc42760fa2bf1ddf941e6f1))

- **agents**: Code review fixes for phase 1 ([#416](https://github.com/UiPath/coder_eval/pull/416),
  [`df9ac29`](https://github.com/UiPath/coder_eval/commit/df9ac2909760d1083fc42760fa2bf1ddf941e6f1))

- **agents**: Code review fixes for phase 2 — uniform ResolvedAgentConfig
  ([#416](https://github.com/UiPath/coder_eval/pull/416),
  [`df9ac29`](https://github.com/UiPath/coder_eval/commit/df9ac2909760d1083fc42760fa2bf1ddf941e6f1))

- **agents**: Code review fixes for phase 3 ([#416](https://github.com/UiPath/coder_eval/pull/416),
  [`df9ac29`](https://github.com/UiPath/coder_eval/commit/df9ac2909760d1083fc42760fa2bf1ddf941e6f1))

- **agents**: Full code review fixes for BYOA SPI
  ([#416](https://github.com/UiPath/coder_eval/pull/416),
  [`df9ac29`](https://github.com/UiPath/coder_eval/commit/df9ac2909760d1083fc42760fa2bf1ddf941e6f1))

- **agents**: Guard duplicate plugin-kind registration + review polish
  ([#416](https://github.com/UiPath/coder_eval/pull/416),
  [`df9ac29`](https://github.com/UiPath/coder_eval/commit/df9ac2909760d1083fc42760fa2bf1ddf941e6f1))

- **ci**: Byoa fixture plugin uses hatchling only-include, not setuptools py-modules
  ([#416](https://github.com/UiPath/coder_eval/pull/416),
  [`df9ac29`](https://github.com/UiPath/coder_eval/commit/df9ac2909760d1083fc42760fa2bf1ddf941e6f1))

### Features

- **agents**: Bring-your-own-agent (BYOA) plugin SPI
  ([#416](https://github.com/UiPath/coder_eval/pull/416),
  [`df9ac29`](https://github.com/UiPath/coder_eval/commit/df9ac2909760d1083fc42760fa2bf1ddf941e6f1))

- **agents**: Phase 1 — entry-point plugin discovery + string-keyed registry
  ([#416](https://github.com/UiPath/coder_eval/pull/416),
  [`df9ac29`](https://github.com/UiPath/coder_eval/commit/df9ac2909760d1083fc42760fa2bf1ddf941e6f1))

- **agents**: Phase 2 — registry-driven agent config dispatch
  ([#416](https://github.com/UiPath/coder_eval/pull/416),
  [`df9ac29`](https://github.com/UiPath/coder_eval/commit/df9ac2909760d1083fc42760fa2bf1ddf941e6f1))

- **agents**: Phase 3 — worked BYOA plugin, live test + CI, docs
  ([#416](https://github.com/UiPath/coder_eval/pull/416),
  [`df9ac29`](https://github.com/UiPath/coder_eval/commit/df9ac2909760d1083fc42760fa2bf1ddf941e6f1))


## v0.4.0 (2026-06-12)

### Bug Fixes

- **docker**: Pin framework entrypoint via docker run --entrypoint
  ([#418](https://github.com/UiPath/coder_eval/pull/418),
  [`a86ad2b`](https://github.com/UiPath/coder_eval/commit/a86ad2b192856d97d58c288992c86556bbce8bfd))

- **docker**: Restore actionable runtime-image guard; reconcile docs
  ([#418](https://github.com/UiPath/coder_eval/pull/418),
  [`a86ad2b`](https://github.com/UiPath/coder_eval/commit/a86ad2b192856d97d58c288992c86556bbce8bfd))

### Features

- **docker**: Pin framework entrypoint via `docker run --entrypoint`
  ([#418](https://github.com/UiPath/coder_eval/pull/418),
  [`a86ad2b`](https://github.com/UiPath/coder_eval/commit/a86ad2b192856d97d58c288992c86556bbce8bfd))

### Refactoring

- **docker**: Coder-eval-specific entrypoint, drop baked ENTRYPOINT
  ([#418](https://github.com/UiPath/coder_eval/pull/418),
  [`a86ad2b`](https://github.com/UiPath/coder_eval/commit/a86ad2b192856d97d58c288992c86556bbce8bfd))


## v0.3.0 (2026-06-12)

### Chores

- Update uv.lock ([#415](https://github.com/UiPath/coder_eval/pull/415),
  [`fdce101`](https://github.com/UiPath/coder_eval/commit/fdce101a750aafa629b43233ec6b0e70c367e6cc))

### Continuous Integration

- Add --strict so no-op release exits non-zero
  ([#417](https://github.com/UiPath/coder_eval/pull/417),
  [`4f6876b`](https://github.com/UiPath/coder_eval/commit/4f6876b12199e70bd5a30d4dbd3cce0a9eff9ced))

- Add conventional commits checker for PR titles and commits
  ([#406](https://github.com/UiPath/coder_eval/pull/406),
  [`ff73e9a`](https://github.com/UiPath/coder_eval/commit/ff73e9aee0e4fce602471b3cedae6ab07b3a2fae))

- Configure git identity before amending release commit
  ([#420](https://github.com/UiPath/coder_eval/pull/420),
  [`37923e8`](https://github.com/UiPath/coder_eval/commit/37923e8a47f2f6f8248812fca54c5c8dc83c6899))

- Fix false-positive release on non-releasable commits
  ([#417](https://github.com/UiPath/coder_eval/pull/417),
  [`4f6876b`](https://github.com/UiPath/coder_eval/commit/4f6876b12199e70bd5a30d4dbd3cce0a9eff9ced))

- Fix orphaned tag after uv.lock amend ([#415](https://github.com/UiPath/coder_eval/pull/415),
  [`fdce101`](https://github.com/UiPath/coder_eval/commit/fdce101a750aafa629b43233ec6b0e70c367e6cc))

- Regenerate uv.lock in the release commit ([#415](https://github.com/UiPath/coder_eval/pull/415),
  [`fdce101`](https://github.com/UiPath/coder_eval/commit/fdce101a750aafa629b43233ec6b0e70c367e6cc))

### Features

- Skill-activation nightly — stratified sampling, per-skill recall, evalboard view
  ([#391](https://github.com/UiPath/coder_eval/pull/391),
  [`ecc0403`](https://github.com/UiPath/coder_eval/commit/ecc04032a20e8350bcabca67fccdc6c3118b494b))

- **activation**: Compute activation score + add Slack line
  ([#391](https://github.com/UiPath/coder_eval/pull/391),
  [`ecc0403`](https://github.com/UiPath/coder_eval/commit/ecc04032a20e8350bcabca67fccdc6c3118b494b))

- **activation**: Enrich case rows + rework the evalboard activation views
  ([#391](https://github.com/UiPath/coder_eval/pull/391),
  [`ecc0403`](https://github.com/UiPath/coder_eval/commit/ecc04032a20e8350bcabca67fccdc6c3118b494b))

- **activation**: Keep activation rows out of run-level metrics
  ([#391](https://github.com/UiPath/coder_eval/pull/391),
  [`ecc0403`](https://github.com/UiPath/coder_eval/commit/ecc04032a20e8350bcabca67fccdc6c3118b494b))

- **activation**: Merge skills + activation into one nightly run
  ([#391](https://github.com/UiPath/coder_eval/pull/391),
  [`ecc0403`](https://github.com/UiPath/coder_eval/commit/ecc04032a20e8350bcabca67fccdc6c3118b494b))

- **activation**: Per-skill recall aggregation + evalboard view
  ([#391](https://github.com/UiPath/coder_eval/pull/391),
  [`ecc0403`](https://github.com/UiPath/coder_eval/commit/ecc04032a20e8350bcabca67fccdc6c3118b494b))

- **activation**: Run 20/skill nightly via --sample-per-stratum
  ([#391](https://github.com/UiPath/coder_eval/pull/391),
  [`ecc0403`](https://github.com/UiPath/coder_eval/commit/ecc04032a20e8350bcabca67fccdc6c3118b494b))

- **activation**: Run as a nested sub-run instead of merging into run.json
  ([#391](https://github.com/UiPath/coder_eval/pull/391),
  [`ecc0403`](https://github.com/UiPath/coder_eval/commit/ecc04032a20e8350bcabca67fccdc6c3118b494b))

- **activation**: Surface activation in the evalboard (front page, run card, dedicated page)
  ([#391](https://github.com/UiPath/coder_eval/pull/391),
  [`ecc0403`](https://github.com/UiPath/coder_eval/commit/ecc04032a20e8350bcabca67fccdc6c3118b494b))

- **dataset**: Stratified random sampling; make --sample random
  ([#391](https://github.com/UiPath/coder_eval/pull/391),
  [`ecc0403`](https://github.com/UiPath/coder_eval/commit/ecc04032a20e8350bcabca67fccdc6c3118b494b))

- **evalboard**: Hide activation rows from run view by default
  ([#391](https://github.com/UiPath/coder_eval/pull/391),
  [`ecc0403`](https://github.com/UiPath/coder_eval/commit/ecc04032a20e8350bcabca67fccdc6c3118b494b))


## v0.2.1 (2026-06-11)

### Bug Fixes

- Pin BYOD smoke template to coder-eval-agent:latest
  ([#401](https://github.com/UiPath/coder_eval/pull/401),
  [`6405391`](https://github.com/UiPath/coder_eval/commit/64053914411ae3b82e888de3b44246a6ea2155b6))

- Sync uv.lock with the 0.2.0 version bump ([#401](https://github.com/UiPath/coder_eval/pull/401),
  [`6405391`](https://github.com/UiPath/coder_eval/commit/64053914411ae3b82e888de3b44246a6ea2155b6))

### Refactoring

- Remove UiPath eval content moved to coder-eval-uipath
  ([#401](https://github.com/UiPath/coder_eval/pull/401),
  [`6405391`](https://github.com/UiPath/coder_eval/commit/64053914411ae3b82e888de3b44246a6ea2155b6))

- Sweep remaining references to moved eval content
  ([#401](https://github.com/UiPath/coder_eval/pull/401),
  [`6405391`](https://github.com/UiPath/coder_eval/commit/64053914411ae3b82e888de3b44246a6ea2155b6))


## v0.2.0 (2026-06-11)

### Bug Fixes

- **sandbox**: Address PR review — restore -p alias, close test gaps, doc deltas
  ([#398](https://github.com/UiPath/coder_eval/pull/398),
  [`cbcfb85`](https://github.com/UiPath/coder_eval/commit/cbcfb857e6604e9758a06ffa81e6b7f982d1c647))

- **sandbox**: Clear stale artifacts on resume re-run; drop CLI aliases; review polish
  ([#398](https://github.com/UiPath/coder_eval/pull/398),
  [`cbcfb85`](https://github.com/UiPath/coder_eval/commit/cbcfb857e6604e9758a06ffa81e6b7f982d1c647))

### Features

- **sandbox**: Explicit --preservation-mode (NONE/MOVE_ON_WRITE/DIRECT_WRITE)
  ([#398](https://github.com/UiPath/coder_eval/pull/398),
  [`cbcfb85`](https://github.com/UiPath/coder_eval/commit/cbcfb857e6604e9758a06ffa81e6b7f982d1c647))

### Testing

- Replace tautological evaluate-mapping test (CodeQL constant-in-conditional)
  ([#398](https://github.com/UiPath/coder_eval/pull/398),
  [`cbcfb85`](https://github.com/UiPath/coder_eval/commit/cbcfb857e6604e9758a06ffa81e6b7f982d1c647))


## v0.1.0 (2026-06-10)

- Initial Release
