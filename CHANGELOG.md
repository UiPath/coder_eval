# CHANGELOG

<!-- version list -->

## v0.11.5 (2026-08-28)

### Bug Fixes

- **opencode**: Bound the post-EOF reap, reap the whole process group, test every failure path
  ([#115](https://github.com/UiPath/coder_eval/pull/115),
  [`5532b87`](https://github.com/UiPath/coder_eval/commit/5532b87ac218f6e413479ad7e2eb8d11d89686b9))

- **opencode**: Close the remaining review findings on the harness
  ([#115](https://github.com/UiPath/coder_eval/pull/115),
  [`5532b87`](https://github.com/UiPath/coder_eval/commit/5532b87ac218f6e413479ad7e2eb8d11d89686b9))

- **opencode**: Gate on captured tokens, canonicalize tool args, pin max_turns
  ([#115](https://github.com/UiPath/coder_eval/pull/115),
  [`5532b87`](https://github.com/UiPath/coder_eval/commit/5532b87ac218f6e413479ad7e2eb8d11d89686b9))

- **opencode**: Inject plugin skills so skill suites measure the skills
  ([#115](https://github.com/UiPath/coder_eval/pull/115),
  [`5532b87`](https://github.com/UiPath/coder_eval/commit/5532b87ac218f6e413479ad7e2eb8d11d89686b9))

- **opencode**: Keep the smoke task out of the CI smoke-pass bucket
  ([#115](https://github.com/UiPath/coder_eval/pull/115),
  [`5532b87`](https://github.com/UiPath/coder_eval/commit/5532b87ac218f6e413479ad7e2eb8d11d89686b9))

- **opencode**: Map apply_patch to Write so GPT-family edits are seen by criteria
  ([#115](https://github.com/UiPath/coder_eval/pull/115),
  [`5532b87`](https://github.com/UiPath/coder_eval/commit/5532b87ac218f6e413479ad7e2eb8d11d89686b9))

- **opencode**: Reap the CLI on every turn exit, test the sandbox env contract
  ([#115](https://github.com/UiPath/coder_eval/pull/115),
  [`5532b87`](https://github.com/UiPath/coder_eval/commit/5532b87ac218f6e413479ad7e2eb8d11d89686b9))

- **opencode**: Spread super() in get_environment_info; guard with CE046
  ([#115](https://github.com/UiPath/coder_eval/pull/115),
  [`5532b87`](https://github.com/UiPath/coder_eval/commit/5532b87ac218f6e413479ad7e2eb8d11d89686b9))

- **opencode**: Typecheck on Windows, satisfy both CodeQL findings
  ([#115](https://github.com/UiPath/coder_eval/pull/115),
  [`5532b87`](https://github.com/UiPath/coder_eval/commit/5532b87ac218f6e413479ad7e2eb8d11d89686b9))

- **plugin**: Close the review's verified gaps — regex blind spot, parallel surfaces, runtime guard
  ([#143](https://github.com/UiPath/coder_eval/pull/143),
  [`c565ebb`](https://github.com/UiPath/coder_eval/commit/c565ebb8c8c7d895489d2ae6e244959ba12470af))

- **plugin**: Correct the skill-reachability path — every generated activation suite reports recall
  0.0 ([#143](https://github.com/UiPath/coder_eval/pull/143),
  [`c565ebb`](https://github.com/UiPath/coder_eval/commit/c565ebb8c8c7d895489d2ae6e244959ba12470af))

- **plugin**: Correct the skill-reachability path, and reuse PR #109's measured descriptions
  ([#143](https://github.com/UiPath/coder_eval/pull/143),
  [`c565ebb`](https://github.com/UiPath/coder_eval/commit/c565ebb8c8c7d895489d2ae6e244959ba12470af))

- **routing**: Decouple simulator route from checker_context.api_route
  ([#144](https://github.com/UiPath/coder_eval/pull/144),
  [`88ff0f0`](https://github.com/UiPath/coder_eval/commit/88ff0f0ff910f4c3d9c6b81c5b2b88356240a3c4))

- **routing**: Reinstate litellm+agent_judge rejection guard
  ([#144](https://github.com/UiPath/coder_eval/pull/144),
  [`88ff0f0`](https://github.com/UiPath/coder_eval/commit/88ff0f0ff910f4c3d9c6b81c5b2b88356240a3c4))

- **routing**: Restore LiteLLM->Claude pin on simulator_route
  ([#144](https://github.com/UiPath/coder_eval/pull/144),
  [`88ff0f0`](https://github.com/UiPath/coder_eval/commit/88ff0f0ff910f4c3d9c6b81c5b2b88356240a3c4))

- **test**: Add CE045, document the plugin-path divergence, unpin a test from ordering
  ([#143](https://github.com/UiPath/coder_eval/pull/143),
  [`c565ebb`](https://github.com/UiPath/coder_eval/commit/c565ebb8c8c7d895489d2ae6e244959ba12470af))

- **utils**: Use explicit concatenation in the plugin-root warning
  ([#143](https://github.com/UiPath/coder_eval/pull/143),
  [`c565ebb`](https://github.com/UiPath/coder_eval/commit/c565ebb8c8c7d895489d2ae6e244959ba12470af))

### Chores

- **opencode**: Standardize on deepseek-v4-pro, drop the flash-0731 rate entry
  ([#115](https://github.com/UiPath/coder_eval/pull/115),
  [`5532b87`](https://github.com/UiPath/coder_eval/commit/5532b87ac218f6e413479ad7e2eb8d11d89686b9))

- **plugin**: Answer "skills only?", fold tags into keywords, add CE044
  ([#141](https://github.com/UiPath/coder_eval/pull/141),
  [`2ae9b7b`](https://github.com/UiPath/coder_eval/commit/2ae9b7badb10831a11d81e703aa5e7a981616ace))

- **plugin**: Give both author objects the same contact address
  ([#141](https://github.com/UiPath/coder_eval/pull/141),
  [`2ae9b7b`](https://github.com/UiPath/coder_eval/commit/2ae9b7badb10831a11d81e703aa5e7a981616ace))

- **plugin**: Give the marketplace owner a contact address
  ([#141](https://github.com/UiPath/coder_eval/pull/141),
  [`2ae9b7b`](https://github.com/UiPath/coder_eval/commit/2ae9b7badb10831a11d81e703aa5e7a981616ace))

- **plugin**: Lead both manifests with skill evaluation, add discovery metadata
  ([#141](https://github.com/UiPath/coder_eval/pull/141),
  [`2ae9b7b`](https://github.com/UiPath/coder_eval/commit/2ae9b7badb10831a11d81e703aa5e7a981616ace))

- **plugin**: Pin both manifests to their published JSON schemas
  ([#141](https://github.com/UiPath/coder_eval/pull/141),
  [`2ae9b7b`](https://github.com/UiPath/coder_eval/commit/2ae9b7badb10831a11d81e703aa5e7a981616ace))

### Documentation

- **opencode**: Note _TERM_GRACE_SECONDS's second role as the post-EOF exit grace
  ([#115](https://github.com/UiPath/coder_eval/pull/115),
  [`5532b87`](https://github.com/UiPath/coder_eval/commit/5532b87ac218f6e413479ad7e2eb8d11d89686b9))

### Features

- **agents**: Add OpenCode harness with opt-in [opencode] extra
  ([#115](https://github.com/UiPath/coder_eval/pull/115),
  [`5532b87`](https://github.com/UiPath/coder_eval/commit/5532b87ac218f6e413479ad7e2eb8d11d89686b9))

- **opencode**: Add require_token_telemetry, an escape hatch for the zero-token guard
  ([#115](https://github.com/UiPath/coder_eval/pull/115),
  [`5532b87`](https://github.com/UiPath/coder_eval/commit/5532b87ac218f6e413479ad7e2eb8d11d89686b9))


## v0.11.4 (2026-08-27)

### Chores

- Add @CarlesUIPath as a code owner ([#140](https://github.com/UiPath/coder_eval/pull/140),
  [`8fff865`](https://github.com/UiPath/coder_eval/commit/8fff8651589b2e43922321c689a66bb3aa41330d))

- Sync PR-review comment allowlist with CODEOWNERS
  ([#140](https://github.com/UiPath/coder_eval/pull/140),
  [`8fff865`](https://github.com/UiPath/coder_eval/commit/8fff8651589b2e43922321c689a66bb3aa41330d))

### Features

- **docker**: Bake litellm extra into the docker image
  ([#142](https://github.com/UiPath/coder_eval/pull/142),
  [`3f4ae36`](https://github.com/UiPath/coder_eval/commit/3f4ae363b15024e2a033cf92ccda6befaef3a8b9))


## v0.11.3 (2026-08-27)

### Bug Fixes

- **checker-context**: Typed model, reject litellm+agent_judge/sim, live test
  ([#137](https://github.com/UiPath/coder_eval/pull/137),
  [`727bb7b`](https://github.com/UiPath/coder_eval/commit/727bb7bee459b43c51af93067907687c5e04cfc8))

- **eval-routing**: Restore DEFAULT_JUDGE_MODEL floor + add litellm judge transport
  ([#137](https://github.com/UiPath/coder_eval/pull/137),
  [`727bb7b`](https://github.com/UiPath/coder_eval/commit/727bb7bee459b43c51af93067907687c5e04cfc8))

- **evalboard**: Exclude carried-forward passes from the wall-clock aggregates
  ([#125](https://github.com/UiPath/coder_eval/pull/125),
  [`dd918e6`](https://github.com/UiPath/coder_eval/commit/dd918e68a454f39c8254f74fe2e4f346cff3a16f))

- **release**: Pin python-semantic-release + GitPython to unbreak version bump
  ([#139](https://github.com/UiPath/coder_eval/pull/139),
  [`ef74014`](https://github.com/UiPath/coder_eval/commit/ef74014d3dd4b389f794e0cba743fc67ed430df8))

- **routing**: Make _resolve_backend_route's match exhaustive
  ([#137](https://github.com/UiPath/coder_eval/pull/137),
  [`727bb7b`](https://github.com/UiPath/coder_eval/commit/727bb7bee459b43c51af93067907687c5e04cfc8))

### Continuous Integration

- Retrigger checks (GH Actions appeared stalled repo-wide)
  ([#137](https://github.com/UiPath/coder_eval/pull/137),
  [`727bb7b`](https://github.com/UiPath/coder_eval/commit/727bb7bee459b43c51af93067907687c5e04cfc8))

- Retrigger checks (previous push did not trigger CI)
  ([#137](https://github.com/UiPath/coder_eval/pull/137),
  [`727bb7b`](https://github.com/UiPath/coder_eval/commit/727bb7bee459b43c51af93067907687c5e04cfc8))

- **fix**: Install litellm extra for pyright, address CodeQL findings
  ([#137](https://github.com/UiPath/coder_eval/pull/137),
  [`727bb7b`](https://github.com/UiPath/coder_eval/commit/727bb7bee459b43c51af93067907687c5e04cfc8))

### Documentation

- **timing**: Trim justification prose from the wall-clock comments
  ([#125](https://github.com/UiPath/coder_eval/pull/125),
  [`dd918e6`](https://github.com/UiPath/coder_eval/commit/dd918e68a454f39c8254f74fe2e4f346cff3a16f))

### Features

- **eval-routing**: Decouple judge/agent_judge backend+model from the agent's own route
  ([#137](https://github.com/UiPath/coder_eval/pull/137),
  [`727bb7b`](https://github.com/UiPath/coder_eval/commit/727bb7bee459b43c51af93067907687c5e04cfc8))

- **eval-routing**: Decouple judge/agent_judge backend+model from the agent's route
  ([#137](https://github.com/UiPath/coder_eval/pull/137),
  [`727bb7b`](https://github.com/UiPath/coder_eval/commit/727bb7bee459b43c51af93067907687c5e04cfc8))

- **evalboard**: Chart seconds per passed task on the overview
  ([#125](https://github.com/UiPath/coder_eval/pull/125),
  [`dd918e6`](https://github.com/UiPath/coder_eval/commit/dd918e68a454f39c8254f74fe2e4f346cff3a16f))

- **evalboard**: Read the time ratio without hovering
  ([#125](https://github.com/UiPath/coder_eval/pull/125),
  [`dd918e6`](https://github.com/UiPath/coder_eval/commit/dd918e68a454f39c8254f74fe2e4f346cff3a16f))

- **evalboard**: Replace the turn-budget signal with time per passed task
  ([#125](https://github.com/UiPath/coder_eval/pull/125),
  [`dd918e6`](https://github.com/UiPath/coder_eval/commit/dd918e68a454f39c8254f74fe2e4f346cff3a16f))

- **evalboard**: Run the wall-clock signal beside the turn budget, behind tabs
  ([#125](https://github.com/UiPath/coder_eval/pull/125),
  [`dd918e6`](https://github.com/UiPath/coder_eval/commit/dd918e68a454f39c8254f74fe2e4f346cff3a16f))

- **litellm-judge**: Support arbitrary litellm kwargs via params/auth
  ([#137](https://github.com/UiPath/coder_eval/pull/137),
  [`727bb7b`](https://github.com/UiPath/coder_eval/commit/727bb7bee459b43c51af93067907687c5e04cfc8))

### Refactoring

- **litellm-judge**: Drop settings coupling, rename auth to env_params
  ([#137](https://github.com/UiPath/coder_eval/pull/137),
  [`727bb7b`](https://github.com/UiPath/coder_eval/commit/727bb7bee459b43c51af93067907687c5e04cfc8))


## v0.11.2 (2026-08-24)

### Bug Fixes

- **docker**: Expand ~ and $VAR in extra_mounts destinations
  ([#128](https://github.com/UiPath/coder_eval/pull/128),
  [`02dbf69`](https://github.com/UiPath/coder_eval/commit/02dbf694705fd3f7de4ce164230a70945f08a1ce))

- **docker**: Reject expansions that inject a ':' into a mount path
  ([#128](https://github.com/UiPath/coder_eval/pull/128),
  [`02dbf69`](https://github.com/UiPath/coder_eval/commit/02dbf694705fd3f7de4ce164230a70945f08a1ce))

### Documentation

- **docker**: Trim the extra_mounts destination comments to scope
  ([#128](https://github.com/UiPath/coder_eval/pull/128),
  [`02dbf69`](https://github.com/UiPath/coder_eval/commit/02dbf694705fd3f7de4ce164230a70945f08a1ce))


## v0.11.1 (2026-08-21)

### Bug Fixes

- **ci**: Pin pip floor to 26.2 to close PYSEC-2026-3721
  ([#133](https://github.com/UiPath/coder_eval/pull/133),
  [`beecedd`](https://github.com/UiPath/coder_eval/commit/beeceddc521957d3cb53f944b7877dada1155548))

- **codex**: Store command output whole in result_summary (+ code-review fixes)
  ([#127](https://github.com/UiPath/coder_eval/pull/127),
  [`3d09f0f`](https://github.com/UiPath/coder_eval/commit/3d09f0f4d966f619405058aca44aa982b1ba265a))

- **deps**: Address PR #133 review — anthropic 1.0.0 dropped temperature kwarg
  ([#133](https://github.com/UiPath/coder_eval/pull/133),
  [`beecedd`](https://github.com/UiPath/coder_eval/commit/beeceddc521957d3cb53f944b7877dada1155548))

- **deps**: Bump anthropic to 1.0.0 and migrate Bedrock judge path to httpx2
  ([#133](https://github.com/UiPath/coder_eval/pull/133),
  [`beecedd`](https://github.com/UiPath/coder_eval/commit/beeceddc521957d3cb53f944b7877dada1155548))

- **deps**: Bump anthropic to 1.0.0, migrate Bedrock judge path to httpx2
  ([#133](https://github.com/UiPath/coder_eval/pull/133),
  [`beecedd`](https://github.com/UiPath/coder_eval/commit/beeceddc521957d3cb53f944b7877dada1155548))

- **deps**: Re-lock pip to 26.2.1 to actually close PYSEC-2026-3721
  ([#133](https://github.com/UiPath/coder_eval/pull/133),
  [`beecedd`](https://github.com/UiPath/coder_eval/commit/beeceddc521957d3cb53f944b7877dada1155548))


## v0.11.0 (2026-08-19)

### Bug Fixes

- **docker**: Restore container access after the DAC cap drop, shield the task dir
  ([#106](https://github.com/UiPath/coder_eval/pull/106),
  [`56bffad`](https://github.com/UiPath/coder_eval/commit/56bffad6acc4c0b32357bd25334bedd403c2f0d0))

- **reference**: Address code review and CodeQL findings
  ([#106](https://github.com/UiPath/coder_eval/pull/106),
  [`56bffad`](https://github.com/UiPath/coder_eval/commit/56bffad6acc4c0b32357bd25334bedd403c2f0d0))

- **reference**: Address PR review — scoring correctness, fail-closed anti-cheat
  ([#106](https://github.com/UiPath/coder_eval/pull/106),
  [`56bffad`](https://github.com/UiPath/coder_eval/commit/56bffad6acc4c0b32357bd25334bedd403c2f0d0))

- **reference**: Clear remaining CodeQL alerts
  ([#106](https://github.com/UiPath/coder_eval/pull/106),
  [`56bffad`](https://github.com/UiPath/coder_eval/commit/56bffad6acc4c0b32357bd25334bedd403c2f0d0))

### Documentation

- **reference**: Record why READ_ONLY_MODE exists, and what is left to wire
  ([#106](https://github.com/UiPath/coder_eval/pull/106),
  [`56bffad`](https://github.com/UiPath/coder_eval/commit/56bffad6acc4c0b32357bd25334bedd403c2f0d0))

### Features

- **reference**: Directory-only references + anti-cheat permission window
  ([#106](https://github.com/UiPath/coder_eval/pull/106),
  [`56bffad`](https://github.com/UiPath/coder_eval/commit/56bffad6acc4c0b32357bd25334bedd403c2f0d0))

### Testing

- **early-stop**: CE036 enforces the live_verdict determinism + monotonicity contract
  ([#126](https://github.com/UiPath/coder_eval/pull/126),
  [`d854004`](https://github.com/UiPath/coder_eval/commit/d854004a5ec4fce0dab16d17d809a93c448fa022))

- **reference**: Skip host-side chmod assertions on Windows
  ([#106](https://github.com/UiPath/coder_eval/pull/106),
  [`56bffad`](https://github.com/UiPath/coder_eval/commit/56bffad6acc4c0b32357bd25334bedd403c2f0d0))


## v0.10.2 (2026-08-18)

### Continuous Integration

- Exclude attestation sidecars from the PyPI artifact-identity assert
  ([#124](https://github.com/UiPath/coder_eval/pull/124),
  [`7d8a771`](https://github.com/UiPath/coder_eval/commit/7d8a771f51fc8694a90f55477d410a55235a4d8a))


## v0.10.1 (2026-08-18)

### Continuous Integration

- Bump gh-action-pypi-publish to v1.14.2 to accept Metadata-Version 2.5
  ([#123](https://github.com/UiPath/coder_eval/pull/123),
  [`a65ee69`](https://github.com/UiPath/coder_eval/commit/a65ee69a65f97bb3e7e2fbff942c2a110b3c07ee))


## v0.10.0 (2026-08-18)

### Bug Fixes

- Code review fixes for the Claude Code plugin marketplace
  ([#82](https://github.com/UiPath/coder_eval/pull/82),
  [`02e5151`](https://github.com/UiPath/coder_eval/commit/02e515106e2a59acc1640642957a28f3f737b436))

- Code review fixes for the plugin generic-adopter plan
  ([#82](https://github.com/UiPath/coder_eval/pull/82),
  [`02e5151`](https://github.com/UiPath/coder_eval/commit/02e515106e2a59acc1640642957a28f3f737b436))

- Code review fixes for the plugin-audit P0/P1 plan
  ([#82](https://github.com/UiPath/coder_eval/pull/82),
  [`02e5151`](https://github.com/UiPath/coder_eval/commit/02e515106e2a59acc1640642957a28f3f737b436))

- Derive the poll loop's exit bound from the turn's actual timeout
  ([#111](https://github.com/UiPath/coder_eval/pull/111),
  [`d3f1432`](https://github.com/UiPath/coder_eval/commit/d3f14327b7d5ee0664b457fc0712ea8a1ad2c1ab))

- Make lint-tasks' read-only rule outlive the frontmatter deny
  ([#82](https://github.com/UiPath/coder_eval/pull/82),
  [`02e5151`](https://github.com/UiPath/coder_eval/commit/02e515106e2a59acc1640642957a28f3f737b436))

- Reconcile the plugin branch with main after rebase
  ([#82](https://github.com/UiPath/coder_eval/pull/82),
  [`02e5151`](https://github.com/UiPath/coder_eval/commit/02e515106e2a59acc1640642957a28f3f737b436))

- Remove redundant asyncio re-import flagged by CodeQL
  ([#111](https://github.com/UiPath/coder_eval/pull/111),
  [`d3f1432`](https://github.com/UiPath/coder_eval/commit/d3f14327b7d5ee0664b457fc0712ea8a1ad2c1ab))

- **agent**: Address PR review — simulator replace mode, preset-aware reports, replace validator
  ([#92](https://github.com/UiPath/coder_eval/pull/92),
  [`fddd3c5`](https://github.com/UiPath/coder_eval/commit/fddd3c55ffa2074061caf1ba5128005df0c5a3b0))

- **agent**: Address PR review — unconditional preset, judge replace seam
  ([#92](https://github.com/UiPath/coder_eval/pull/92),
  [`fddd3c5`](https://github.com/UiPath/coder_eval/commit/fddd3c55ffa2074061caf1ba5128005df0c5a3b0))

- **agent**: Align Codex system_prompt with the append-only contract
  ([#92](https://github.com/UiPath/coder_eval/pull/92),
  [`fddd3c5`](https://github.com/UiPath/coder_eval/commit/fddd3c55ffa2074061caf1ba5128005df0c5a3b0))

- **agent**: Append system_prompt to the Claude Code preset instead of replacing
  ([#92](https://github.com/UiPath/coder_eval/pull/92),
  [`fddd3c5`](https://github.com/UiPath/coder_eval/commit/fddd3c55ffa2074061caf1ba5128005df0c5a3b0))

- **agent**: Reject a blank system_prompt_file under replace mode
  ([#92](https://github.com/UiPath/coder_eval/pull/92),
  [`fddd3c5`](https://github.com/UiPath/coder_eval/commit/fddd3c55ffa2074061caf1ba5128005df0c5a3b0))

- **agent**: Resolve system_prompt_file atomically and reject blank prompts
  ([#92](https://github.com/UiPath/coder_eval/pull/92),
  [`fddd3c5`](https://github.com/UiPath/coder_eval/commit/fddd3c55ffa2074061caf1ba5128005df0c5a3b0))

- **antigravity**: Poll for backgrounded work instead of grading it incomplete
  ([#111](https://github.com/UiPath/coder_eval/pull/111),
  [`d3f1432`](https://github.com/UiPath/coder_eval/commit/d3f14327b7d5ee0664b457fc0712ea8a1ad2c1ab))

- **ci**: Address PR #81 review — undefined step output, dead job gate, CE035
  ([#81](https://github.com/UiPath/coder_eval/pull/81),
  [`9cc45da`](https://github.com/UiPath/coder_eval/commit/9cc45daf04c86bb0d0e50779927b72878554bb76))

- **ci**: Close the five gate-correctness findings from the code review
  ([#81](https://github.com/UiPath/coder_eval/pull/81),
  [`9cc45da`](https://github.com/UiPath/coder_eval/commit/9cc45daf04c86bb0d0e50779927b72878554bb76))

- **ci**: Code review fixes for the published-action verification
  ([#81](https://github.com/UiPath/coder_eval/pull/81),
  [`9cc45da`](https://github.com/UiPath/coder_eval/commit/9cc45daf04c86bb0d0e50779927b72878554bb76))

- **ci**: Fail the published-action gate on a run-limit breach
  ([#81](https://github.com/UiPath/coder_eval/pull/81),
  [`9cc45da`](https://github.com/UiPath/coder_eval/commit/9cc45daf04c86bb0d0e50779927b72878554bb76))

- **ci**: Harden promote ordering and stop preflight misdiagnosing healthy lag
  ([#81](https://github.com/UiPath/coder_eval/pull/81),
  [`9cc45da`](https://github.com/UiPath/coder_eval/commit/9cc45daf04c86bb0d0e50779927b72878554bb76))

- **claude**: Make Claude speak English, not Claudish
  ([#118](https://github.com/UiPath/coder_eval/pull/118),
  [`5006c91`](https://github.com/UiPath/coder_eval/commit/5006c914347d6bdbc94f6f7c70384903028ffd50))

- **cli-called**: Move alternation to verb_any_of, close review findings
  ([#103](https://github.com/UiPath/coder_eval/pull/103),
  [`f7e9fda`](https://github.com/UiPath/coder_eval/commit/f7e9fda4d09d97654821bee24c61ecd76838277a))

- **codex**: Fold sub-agent tokens on a turn-cap stop
  ([#110](https://github.com/UiPath/coder_eval/pull/110),
  [`12c5031`](https://github.com/UiPath/coder_eval/commit/12c5031078c64b363f01a1a92803cf3fa06ac400))

- **codex**: Report per-turn tokens instead of the thread-cumulative total
  ([#113](https://github.com/UiPath/coder_eval/pull/113),
  [`80f3523`](https://github.com/UiPath/coder_eval/commit/80f352316813676d8fffb3b9d8cb30d7fcd7f9c2))

- **deps**: Bump sqlparse 0.5.5 -> 0.6.0 to clear the pip-audit gate
  ([#120](https://github.com/UiPath/coder_eval/pull/120),
  [`ea5a3fc`](https://github.com/UiPath/coder_eval/commit/ea5a3fc89c3448bfab723b573ea18e11db4184ec))

- **deps**: Bump sqlparse to 0.6.0 and close pre/post-run subprocess transports
  ([#120](https://github.com/UiPath/coder_eval/pull/120),
  [`ea5a3fc`](https://github.com/UiPath/coder_eval/commit/ea5a3fc89c3448bfab723b573ea18e11db4184ec))

- **evalboard**: Address review findings on the Scribe source layer
  ([#116](https://github.com/UiPath/coder_eval/pull/116),
  [`d6f8d7b`](https://github.com/UiPath/coder_eval/commit/d6f8d7bd44c3a48109e745fecbe2ac5c7ce2451d))

- **evaluation**: Harden post-failure evidence
  ([#119](https://github.com/UiPath/coder_eval/pull/119),
  [`636e87d`](https://github.com/UiPath/coder_eval/commit/636e87d5cbf28fe1ade30876a7251cbb7b24d950))

- **orchestrator**: Close pre/post-run subprocess transports so Windows CI stops leaking
  ([#120](https://github.com/UiPath/coder_eval/pull/120),
  [`ea5a3fc`](https://github.com/UiPath/coder_eval/commit/ea5a3fc89c3448bfab723b573ea18e11db4184ec))

- **plugin**: Address PR #82 review — reachable activation suites, least-privilege CI
  ([#82](https://github.com/UiPath/coder_eval/pull/82),
  [`02e5151`](https://github.com/UiPath/coder_eval/commit/02e515106e2a59acc1640642957a28f3f737b436))

- **plugin**: Address the three PR #82 findings left open
  ([#82](https://github.com/UiPath/coder_eval/pull/82),
  [`02e5151`](https://github.com/UiPath/coder_eval/commit/02e515106e2a59acc1640642957a28f3f737b436))

- **plugin**: Make analyze compute its numbers, and weight smoke criteria honestly
  ([#82](https://github.com/UiPath/coder_eval/pull/82),
  [`02e5151`](https://github.com/UiPath/coder_eval/commit/02e515106e2a59acc1640642957a28f3f737b436))

- **plugin**: Name the tautological-criterion trap in init and task
  ([#82](https://github.com/UiPath/coder_eval/pull/82),
  [`02e5151`](https://github.com/UiPath/coder_eval/commit/02e515106e2a59acc1640642957a28f3f737b436))

- **plugin**: Wire the skill source into the ci skill's scheduled drift run
  ([#82](https://github.com/UiPath/coder_eval/pull/82),
  [`02e5151`](https://github.com/UiPath/coder_eval/commit/02e515106e2a59acc1640642957a28f3f737b436))

- **tasks**: Armed positives must require success, guarded by CE034
  ([#82](https://github.com/UiPath/coder_eval/pull/82),
  [`02e5151`](https://github.com/UiPath/coder_eval/commit/02e515106e2a59acc1640642957a28f3f737b436))

- **test**: Resolve bash by absolute path so Windows CI stops hitting WSL
  ([#81](https://github.com/UiPath/coder_eval/pull/81),
  [`9cc45da`](https://github.com/UiPath/coder_eval/commit/9cc45daf04c86bb0d0e50779927b72878554bb76))

### Chores

- Reconcile the published-action verification with main
  ([#81](https://github.com/UiPath/coder_eval/pull/81),
  [`9cc45da`](https://github.com/UiPath/coder_eval/commit/9cc45daf04c86bb0d0e50779927b72878554bb76))

- Renumber a rebase-collided lint-rule candidate
  ([#81](https://github.com/UiPath/coder_eval/pull/81),
  [`9cc45da`](https://github.com/UiPath/coder_eval/commit/9cc45daf04c86bb0d0e50779927b72878554bb76))

- **agents**: Drop config_support and the Antigravity tool mapping
  ([#110](https://github.com/UiPath/coder_eval/pull/110),
  [`12c5031`](https://github.com/UiPath/coder_eval/commit/12c5031078c64b363f01a1a92803cf3fa06ac400))

- **agents**: Drop the system_prompt changes from this PR
  ([#110](https://github.com/UiPath/coder_eval/pull/110),
  [`12c5031`](https://github.com/UiPath/coder_eval/commit/12c5031078c64b363f01a1a92803cf3fa06ac400))

- **plugin**: Pin plugin.json to 0.9.6 after rebasing onto the release
  ([#82](https://github.com/UiPath/coder_eval/pull/82),
  [`02e5151`](https://github.com/UiPath/coder_eval/commit/02e515106e2a59acc1640642957a28f3f737b436))

- **plugin**: Regenerate the bundled criteria reference
  ([#103](https://github.com/UiPath/coder_eval/pull/103),
  [`f7e9fda`](https://github.com/UiPath/coder_eval/commit/f7e9fda4d09d97654821bee24c61ecd76838277a))

### Code Style

- Sort claude_agent_sdk.types import ([#92](https://github.com/UiPath/coder_eval/pull/92),
  [`fddd3c5`](https://github.com/UiPath/coder_eval/commit/fddd3c55ffa2074061caf1ba5128005df0c5a3b0))

### Continuous Integration

- **release**: Promote v0 only after PyPI publish, verify the published action
  ([#81](https://github.com/UiPath/coder_eval/pull/81),
  [`9cc45da`](https://github.com/UiPath/coder_eval/commit/9cc45daf04c86bb0d0e50779927b72878554bb76))

### Documentation

- Add Tutorial 07 for the plugin, and fix two gaps in PLUGIN.md
  ([#82](https://github.com/UiPath/coder_eval/pull/82),
  [`02e5151`](https://github.com/UiPath/coder_eval/commit/02e515106e2a59acc1640642957a28f3f737b436))

- Defer one harness candidate from the plugin-audit run
  ([#82](https://github.com/UiPath/coder_eval/pull/82),
  [`02e5151`](https://github.com/UiPath/coder_eval/commit/02e515106e2a59acc1640642957a28f3f737b436))

- Rework Tutorial 07 after review — accuracy fixes and far less narration
  ([#82](https://github.com/UiPath/coder_eval/pull/82),
  [`02e5151`](https://github.com/UiPath/coder_eval/commit/02e515106e2a59acc1640642957a28f3f737b436))

- Stop teaching the recursive task glob the ci skill forbids
  ([#82](https://github.com/UiPath/coder_eval/pull/82),
  [`02e5151`](https://github.com/UiPath/coder_eval/commit/02e515106e2a59acc1640642957a28f3f737b436))

- Use current-generation models in examples ([#110](https://github.com/UiPath/coder_eval/pull/110),
  [`12c5031`](https://github.com/UiPath/coder_eval/commit/12c5031078c64b363f01a1a92803cf3fa06ac400))

- **claude**: Drop the config_support contract from the repo guide
  ([#110](https://github.com/UiPath/coder_eval/pull/110),
  [`12c5031`](https://github.com/UiPath/coder_eval/commit/12c5031078c64b363f01a1a92803cf3fa06ac400))

- **cli-called**: Drop the hardcoded subcommand count
  ([#103](https://github.com/UiPath/coder_eval/pull/103),
  [`f7e9fda`](https://github.com/UiPath/coder_eval/commit/f7e9fda4d09d97654821bee24c61ecd76838277a))

- **cli-called**: Fix the guide's orphaned exact_positional prerequisites
  ([#103](https://github.com/UiPath/coder_eval/pull/103),
  [`f7e9fda`](https://github.com/UiPath/coder_eval/commit/f7e9fda4d09d97654821bee24c61ecd76838277a))

- **cli-called**: Fix two unclear field descriptions
  ([#103](https://github.com/UiPath/coder_eval/pull/103),
  [`f7e9fda`](https://github.com/UiPath/coder_eval/commit/f7e9fda4d09d97654821bee24c61ecd76838277a))

- **cli-called**: Name the bug the detail renderer's source avoids
  ([#103](https://github.com/UiPath/coder_eval/pull/103),
  [`f7e9fda`](https://github.com/UiPath/coder_eval/commit/f7e9fda4d09d97654821bee24c61ecd76838277a))

- **cli-called**: Put the offset comment's two reasons on their own lines
  ([#103](https://github.com/UiPath/coder_eval/pull/103),
  [`f7e9fda`](https://github.com/UiPath/coder_eval/commit/f7e9fda4d09d97654821bee24c61ecd76838277a))

- **cli-called**: Trim comments to the whys ([#103](https://github.com/UiPath/coder_eval/pull/103),
  [`f7e9fda`](https://github.com/UiPath/coder_eval/commit/f7e9fda4d09d97654821bee24c61ecd76838277a))

- **parity**: State the real final_status of a capped run
  ([#110](https://github.com/UiPath/coder_eval/pull/110),
  [`12c5031`](https://github.com/UiPath/coder_eval/commit/12c5031078c64b363f01a1a92803cf3fa06ac400))

- **run-limits**: Keep the contract on the page, the measurements in the PR
  ([#110](https://github.com/UiPath/coder_eval/pull/110),
  [`12c5031`](https://github.com/UiPath/coder_eval/commit/12c5031078c64b363f01a1a92803cf3fa06ac400))

- **run-limits**: Re-measure the antigravity timeout case after the poll loop
  ([#110](https://github.com/UiPath/coder_eval/pull/110),
  [`12c5031`](https://github.com/UiPath/coder_eval/commit/12c5031078c64b363f01a1a92803cf3fa06ac400))

- **run-limits**: Record the measured cross-harness parity results
  ([#110](https://github.com/UiPath/coder_eval/pull/110),
  [`12c5031`](https://github.com/UiPath/coder_eval/commit/12c5031078c64b363f01a1a92803cf3fa06ac400))

### Features

- **agent**: Emit system_prompt_semantics from the Agent base
  ([#92](https://github.com/UiPath/coder_eval/pull/92),
  [`fddd3c5`](https://github.com/UiPath/coder_eval/commit/fddd3c55ffa2074061caf1ba5128005df0c5a3b0))

- **agent**: Record system_prompt_semantics marker in environment_info
  ([#92](https://github.com/UiPath/coder_eval/pull/92),
  [`fddd3c5`](https://github.com/UiPath/coder_eval/commit/fddd3c55ffa2074061caf1ba5128005df0c5a3b0))

- **agents**: Honor run_limits.max_turns on codex and antigravity
  ([#110](https://github.com/UiPath/coder_eval/pull/110),
  [`12c5031`](https://github.com/UiPath/coder_eval/commit/12c5031078c64b363f01a1a92803cf3fa06ac400))

- **agents**: Make a base-config field mean the same thing on every harness
  ([#110](https://github.com/UiPath/coder_eval/pull/110),
  [`12c5031`](https://github.com/UiPath/coder_eval/commit/12c5031078c64b363f01a1a92803cf3fa06ac400))

- **cli-called**: Accept a list of verb spellings
  ([#103](https://github.com/UiPath/coder_eval/pull/103),
  [`f7e9fda`](https://github.com/UiPath/coder_eval/commit/f7e9fda4d09d97654821bee24c61ecd76838277a))

- **cli-called**: Accept alternative verbs via verb_any_of
  ([#103](https://github.com/UiPath/coder_eval/pull/103),
  [`f7e9fda`](https://github.com/UiPath/coder_eval/commit/f7e9fda4d09d97654821bee24c61ecd76838277a))

- **cli-called**: Add exact_positional to pin the argument tail
  ([#103](https://github.com/UiPath/coder_eval/pull/103),
  [`f7e9fda`](https://github.com/UiPath/coder_eval/commit/f7e9fda4d09d97654821bee24c61ecd76838277a))

- **evalboard**: Add a Scribe tab, reading the Autopilot suite's own blob container
  ([#116](https://github.com/UiPath/coder_eval/pull/116),
  [`d6f8d7b`](https://github.com/UiPath/coder_eval/commit/d6f8d7bd44c3a48109e745fecbe2ac5c7ce2451d))

- **evaluation**: Preserve criteria after agent failures
  ([#119](https://github.com/UiPath/coder_eval/pull/119),
  [`636e87d`](https://github.com/UiPath/coder_eval/commit/636e87d5cbf28fe1ade30876a7251cbb7b24d950))

- **lint**: CE026 clause 4 — snippet `with:` keys must be real action inputs
  ([#82](https://github.com/UiPath/coder_eval/pull/82),
  [`02e5151`](https://github.com/UiPath/coder_eval/commit/02e515106e2a59acc1640642957a28f3f737b436))

- **plugin**: 1/5 — the bundled criteria reference explains optional fields
  ([#82](https://github.com/UiPath/coder_eval/pull/82),
  [`02e5151`](https://github.com/UiPath/coder_eval/commit/02e515106e2a59acc1640642957a28f3f737b436))

- **plugin**: 1/6 — discover the eval tree instead of assuming tasks/ and runs/latest
  ([#82](https://github.com/UiPath/coder_eval/pull/82),
  [`02e5151`](https://github.com/UiPath/coder_eval/commit/02e515106e2a59acc1640642957a28f3f737b436))

- **plugin**: 1/6 — marketplace + coder-eval plugin skeleton
  ([#82](https://github.com/UiPath/coder_eval/pull/82),
  [`02e5151`](https://github.com/UiPath/coder_eval/commit/02e515106e2a59acc1640642957a28f3f737b436))

- **plugin**: 2/5 — shared adversarial task rubric, applied by `task`
  ([#82](https://github.com/UiPath/coder_eval/pull/82),
  [`02e5151`](https://github.com/UiPath/coder_eval/commit/02e515106e2a59acc1640642957a28f3f737b436))

- **plugin**: 2/6 — analyze reads the run's actual schema, not one generation's
  ([#82](https://github.com/UiPath/coder_eval/pull/82),
  [`02e5151`](https://github.com/UiPath/coder_eval/commit/02e515106e2a59acc1640642957a28f3f737b436))

- **plugin**: 2/6 — generate the bundled criteria reference, guard it with CE032
  ([#82](https://github.com/UiPath/coder_eval/pull/82),
  [`02e5151`](https://github.com/UiPath/coder_eval/commit/02e515106e2a59acc1640642957a28f3f737b436))

- **plugin**: 3/5 — a real run becomes part of done in `task`
  ([#82](https://github.com/UiPath/coder_eval/pull/82),
  [`02e5151`](https://github.com/UiPath/coder_eval/commit/02e515106e2a59acc1640642957a28f3f737b436))

- **plugin**: 3/6 — resolve the version a project pins before validating anything
  ([#82](https://github.com/UiPath/coder_eval/pull/82),
  [`02e5151`](https://github.com/UiPath/coder_eval/commit/02e515106e2a59acc1640642957a28f3f737b436))

- **plugin**: 3/6 — skill-check skill and the canonical activation suite
  ([#82](https://github.com/UiPath/coder_eval/pull/82),
  [`02e5151`](https://github.com/UiPath/coder_eval/commit/02e515106e2a59acc1640642957a28f3f737b436))

- **plugin**: 4/5 — `/coder-eval:lint-tasks`, a read-only reviewer of existing tasks
  ([#82](https://github.com/UiPath/coder_eval/pull/82),
  [`02e5151`](https://github.com/UiPath/coder_eval/commit/02e515106e2a59acc1640642957a28f3f737b436))

- **plugin**: 4/6 — init and task skills ([#82](https://github.com/UiPath/coder_eval/pull/82),
  [`02e5151`](https://github.com/UiPath/coder_eval/commit/02e515106e2a59acc1640642957a28f3f737b436))

- **plugin**: 4/6 — look before you write: skill-check, init, lint-tasks
  ([#82](https://github.com/UiPath/coder_eval/pull/82),
  [`02e5151`](https://github.com/UiPath/coder_eval/commit/02e515106e2a59acc1640642957a28f3f737b436))

- **plugin**: 5/5 — activation budgets in `skill-check`, layer routing in `analyze`
  ([#82](https://github.com/UiPath/coder_eval/pull/82),
  [`02e5151`](https://github.com/UiPath/coder_eval/commit/02e515106e2a59acc1640642957a28f3f737b436))

- **plugin**: 5/6 — analyze and ci skills ([#82](https://github.com/UiPath/coder_eval/pull/82),
  [`02e5151`](https://github.com/UiPath/coder_eval/commit/02e515106e2a59acc1640642957a28f3f737b436))

- **plugin**: 5/6 — repo convention wins, and the CI gate stops measuring the wrong set
  ([#82](https://github.com/UiPath/coder_eval/pull/82),
  [`02e5151`](https://github.com/UiPath/coder_eval/commit/02e515106e2a59acc1640642957a28f3f737b436))

- **plugin**: 6/6 — document the criterion aliases the loader accepts, from the models
  ([#82](https://github.com/UiPath/coder_eval/pull/82),
  [`02e5151`](https://github.com/UiPath/coder_eval/commit/02e515106e2a59acc1640642957a28f3f737b436))

- **plugin**: 6/6 — validate the plugin in CI, extend CE026, document it
  ([#82](https://github.com/UiPath/coder_eval/pull/82),
  [`02e5151`](https://github.com/UiPath/coder_eval/commit/02e515106e2a59acc1640642957a28f3f737b436))

- **plugin**: CLI-driving skills offer to install coder-eval, asking first
  ([#82](https://github.com/UiPath/coder_eval/pull/82),
  [`02e5151`](https://github.com/UiPath/coder_eval/commit/02e515106e2a59acc1640642957a28f3f737b436))

- **plugin**: Ship coder_eval as a Claude Code plugin + marketplace
  ([#82](https://github.com/UiPath/coder_eval/pull/82),
  [`02e5151`](https://github.com/UiPath/coder_eval/commit/02e515106e2a59acc1640642957a28f3f737b436))

### Refactoring

- Retire the repo-local twins of the plugin's authoring skills
  ([#82](https://github.com/UiPath/coder_eval/pull/82),
  [`02e5151`](https://github.com/UiPath/coder_eval/commit/02e515106e2a59acc1640642957a28f3f737b436))

- **agent**: Resolve the system prompt to a value, not a mode string
  ([#92](https://github.com/UiPath/coder_eval/pull/92),
  [`fddd3c5`](https://github.com/UiPath/coder_eval/commit/fddd3c55ffa2074061caf1ba5128005df0c5a3b0))

- **cli-called**: Collapse the pairwise verb check to itertools.combinations
  ([#103](https://github.com/UiPath/coder_eval/pull/103),
  [`f7e9fda`](https://github.com/UiPath/coder_eval/commit/f7e9fda4d09d97654821bee24c61ecd76838277a))

- **plugin**: Rename skill-check to check-skill, and write the naming rule down
  ([#82](https://github.com/UiPath/coder_eval/pull/82),
  [`02e5151`](https://github.com/UiPath/coder_eval/commit/02e515106e2a59acc1640642957a28f3f737b436))

- **reports**: Read the recorded prompt regime instead of sniffing its shape
  ([#92](https://github.com/UiPath/coder_eval/pull/92),
  [`fddd3c5`](https://github.com/UiPath/coder_eval/commit/fddd3c55ffa2074061caf1ba5128005df0c5a3b0))

### Testing

- Defer the plugin-skill repo-file containment guard
  ([#82](https://github.com/UiPath/coder_eval/pull/82),
  [`02e5151`](https://github.com/UiPath/coder_eval/commit/02e515106e2a59acc1640642957a28f3f737b436))

- **antigravity**: Clear the CodeQL findings on the fake SDK helper
  ([#110](https://github.com/UiPath/coder_eval/pull/110),
  [`12c5031`](https://github.com/UiPath/coder_eval/commit/12c5031078c64b363f01a1a92803cf3fa06ac400))

- **antigravity**: Pin the SDK half of the env seam contract
  ([#110](https://github.com/UiPath/coder_eval/pull/110),
  [`12c5031`](https://github.com/UiPath/coder_eval/commit/12c5031078c64b363f01a1a92803cf3fa06ac400))

- **plugin**: Make a new skill declare whether it needs eval-root discovery
  ([#82](https://github.com/UiPath/coder_eval/pull/82),
  [`02e5151`](https://github.com/UiPath/coder_eval/commit/02e515106e2a59acc1640642957a28f3f737b436))

- **run-limits**: Add cross-harness max_turns / turn_timeout fixtures
  ([#110](https://github.com/UiPath/coder_eval/pull/110),
  [`12c5031`](https://github.com/UiPath/coder_eval/commit/12c5031078c64b363f01a1a92803cf3fa06ac400))

- **run-limits**: Make the max_turns fixture assert the cap bound
  ([#110](https://github.com/UiPath/coder_eval/pull/110),
  [`12c5031`](https://github.com/UiPath/coder_eval/commit/12c5031078c64b363f01a1a92803cf3fa06ac400))

- **run-limits**: Tag the parity fixtures ([#110](https://github.com/UiPath/coder_eval/pull/110),
  [`12c5031`](https://github.com/UiPath/coder_eval/commit/12c5031078c64b363f01a1a92803cf3fa06ac400))


## v0.9.6 (2026-08-11)

### Bug Fixes

- Code review fixes for evalboard path-to-ga stale tags and mature passes
  ([#94](https://github.com/UiPath/coder_eval/pull/94),
  [`ce006c1`](https://github.com/UiPath/coder_eval/commit/ce006c19b594701e90aec1d63cae15b337c0e06d))

- Review round 2 — gate evalboard in CI, correct GPT-5.6 rates
  ([#94](https://github.com/UiPath/coder_eval/pull/94),
  [`ce006c1`](https://github.com/UiPath/coder_eval/commit/ce006c19b594701e90aec1d63cae15b337c0e06d))

- **ci**: Correct defects in the uipath runner migration
  ([#86](https://github.com/UiPath/coder_eval/pull/86),
  [`57556af`](https://github.com/UiPath/coder_eval/commit/57556af6e47bffcd5fbbb708b9f8b399cceea4c7))

- **criteria**: Make glob path resolution literal-first and ignore-filtered
  ([#65](https://github.com/UiPath/coder_eval/pull/65),
  [`b3bba2b`](https://github.com/UiPath/coder_eval/commit/b3bba2b6c00e3772351b28bf63d5cde9e97b8e7e))

- **criteria**: Split clustered short flags and keep negative numbers positional
  ([#73](https://github.com/UiPath/coder_eval/pull/73),
  [`a7ec3ea`](https://github.com/UiPath/coder_eval/commit/a7ec3eababc907a0220a598d974540eb579a538c))

- **evalboard**: 1/3 — drop de-tagged tasks and score only executed runs
  ([#94](https://github.com/UiPath/coder_eval/pull/94),
  [`ce006c1`](https://github.com/UiPath/coder_eval/commit/ce006c19b594701e90aec1d63cae15b337c0e06d))

- **evalboard**: Path-to-GA shows only still-tagged tasks, scored on runs that executed
  ([#94](https://github.com/UiPath/coder_eval/pull/94),
  [`ce006c1`](https://github.com/UiPath/coder_eval/commit/ce006c19b594701e90aec1d63cae15b337c0e06d))

- **evalboard**: Resync lib/pricing.ts with the authoritative pricing.py table
  ([#94](https://github.com/UiPath/coder_eval/pull/94),
  [`ce006c1`](https://github.com/UiPath/coder_eval/commit/ce006c19b594701e90aec1d63cae15b337c0e06d))

- **sandbox**: Close the remaining record_cli findings from #73
  ([#73](https://github.com/UiPath/coder_eval/pull/73),
  [`a7ec3ea`](https://github.com/UiPath/coder_eval/commit/a7ec3eababc907a0220a598d974540eb579a538c))

- **sandbox**: Repair record_cli defects found reviewing #73
  ([#73](https://github.com/UiPath/coder_eval/pull/73),
  [`a7ec3ea`](https://github.com/UiPath/coder_eval/commit/a7ec3eababc907a0220a598d974540eb579a538c))

- **sandbox**: Stop the recorder dir defeating the PLUGIN_TOOLS_DIR pin
  ([#73](https://github.com/UiPath/coder_eval/pull/73),
  [`a7ec3ea`](https://github.com/UiPath/coder_eval/commit/a7ec3eababc907a0220a598d974540eb579a538c))

### Chores

- Change to centralized managed GitHub pool ([#86](https://github.com/UiPath/coder_eval/pull/86),
  [`57556af`](https://github.com/UiPath/coder_eval/commit/57556af6e47bffcd5fbbb708b9f8b399cceea4c7))

### Documentation

- **harness**: Defer four TS-side evalboard invariants from the path-to-ga fix
  ([#94](https://github.com/UiPath/coder_eval/pull/94),
  [`ce006c1`](https://github.com/UiPath/coder_eval/commit/ce006c19b594701e90aec1d63cae15b337c0e06d))

### Features

- **criteria**: Accept glob patterns in criterion path fields
  ([#65](https://github.com/UiPath/coder_eval/pull/65),
  [`b3bba2b`](https://github.com/UiPath/coder_eval/commit/b3bba2b6c00e3772351b28bf63d5cde9e97b8e7e))

- **evalboard**: 2/3 — surface last-seen and maturity on the Path-to-GA table
  ([#94](https://github.com/UiPath/coder_eval/pull/94),
  [`ce006c1`](https://github.com/UiPath/coder_eval/commit/ce006c19b594701e90aec1d63cae15b337c0e06d))

- **sandbox**: Generate CLI recording shims via record_cli
  ([#73](https://github.com/UiPath/coder_eval/pull/73),
  [`a7ec3ea`](https://github.com/UiPath/coder_eval/commit/a7ec3eababc907a0220a598d974540eb579a538c))


## v0.9.5 (2026-08-05)

### Bug Fixes

- **command-executed**: Keep whole argv-joined payload in shell unwrap
  ([#77](https://github.com/UiPath/coder_eval/pull/77),
  [`7abd080`](https://github.com/UiPath/coder_eval/commit/7abd08098146067890e16740c37239c2e0009a24))

- **command-executed**: Match patterns against shell-normalized commands
  ([#77](https://github.com/UiPath/coder_eval/pull/77),
  [`7abd080`](https://github.com/UiPath/coder_eval/commit/7abd08098146067890e16740c37239c2e0009a24))

- **command-executed**: Narrow command param to str before shell-normalizing
  ([#77](https://github.com/UiPath/coder_eval/pull/77),
  [`7abd080`](https://github.com/UiPath/coder_eval/commit/7abd08098146067890e16740c37239c2e0009a24))

- **command-executed**: Recognize shell wrappers by predicate, not allowlist
  ([#77](https://github.com/UiPath/coder_eval/pull/77),
  [`7abd080`](https://github.com/UiPath/coder_eval/commit/7abd08098146067890e16740c37239c2e0009a24))

- **criteria**: Add present predicate so asserting a switch cannot weaken a guard
  ([#72](https://github.com/UiPath/coder_eval/pull/72),
  [`8574ded`](https://github.com/UiPath/coder_eval/commit/8574dedb02d6e9ab883b042934f89297591e8abd))

- **criteria**: Make cli_called guards fail loud instead of vacuously passing
  ([#72](https://github.com/UiPath/coder_eval/pull/72),
  [`8574ded`](https://github.com/UiPath/coder_eval/commit/8574dedb02d6e9ab883b042934f89297591e8abd))

- **criteria**: Stop ignore_flags re-opening the guard false-PASS
  ([#72](https://github.com/UiPath/coder_eval/pull/72),
  [`8574ded`](https://github.com/UiPath/coder_eval/commit/8574dedb02d6e9ab883b042934f89297591e8abd))

- **early-stop**: Address PR review — trajectory parity, reason determinism, doc restore
  ([#78](https://github.com/UiPath/coder_eval/pull/78),
  [`4cf8092`](https://github.com/UiPath/coder_eval/commit/4cf80920422e25fec663ef93e483902bdeffad24))

- **lint**: Derive CE030 criteria from the source union literal, not runtime
  ([#77](https://github.com/UiPath/coder_eval/pull/77),
  [`7abd080`](https://github.com/UiPath/coder_eval/commit/7abd08098146067890e16740c37239c2e0009a24))

- **lint**: Enumerate in-tree criteria by module attribute, not the union
  ([#77](https://github.com/UiPath/coder_eval/pull/77),
  [`7abd080`](https://github.com/UiPath/coder_eval/commit/7abd08098146067890e16740c37239c2e0009a24))

- **lint**: Scope CE030 criterion parity to in-tree criteria only
  ([#77](https://github.com/UiPath/coder_eval/pull/77),
  [`7abd080`](https://github.com/UiPath/coder_eval/commit/7abd08098146067890e16740c37239c2e0009a24))

- **reports**: Explicit return on every early_stop_gate_note path (CodeQL py/mixed-returns)
  ([#78](https://github.com/UiPath/coder_eval/pull/78),
  [`4cf8092`](https://github.com/UiPath/coder_eval/commit/4cf80920422e25fec663ef93e483902bdeffad24))

- **reports**: Pre-initialize the gate note so CodeQL sees it bound on every path
  ([#78](https://github.com/UiPath/coder_eval/pull/78),
  [`4cf8092`](https://github.com/UiPath/coder_eval/commit/4cf80920422e25fec663ef93e483902bdeffad24))

### Chores

- **deps-dev**: Bump postcss from 8.5.18 to 8.5.23 in /evalboard
  ([#75](https://github.com/UiPath/coder_eval/pull/75),
  [`cdced15`](https://github.com/UiPath/coder_eval/commit/cdced152dea732096dcff939c45e7d7c927c2a5a))

### Documentation

- Surface the Marketplace listing and make the Action quickstarts self-sufficient
  ([#80](https://github.com/UiPath/coder_eval/pull/80),
  [`401245a`](https://github.com/UiPath/coder_eval/commit/401245ad14055c1da5d7b594e506686c59cedce7))

- **command-executed**: Document shell-normalization contract + gate it (CE030)
  ([#77](https://github.com/UiPath/coder_eval/pull/77),
  [`7abd080`](https://github.com/UiPath/coder_eval/commit/7abd08098146067890e16740c37239c2e0009a24))

### Features

- **criteria**: Add cli_called for structured invocation matching
  ([#72](https://github.com/UiPath/coder_eval/pull/72),
  [`8574ded`](https://github.com/UiPath/coder_eval/commit/8574dedb02d6e9ab883b042934f89297591e8abd))

- **criteria**: Match a flag across spellings with FlagMatch.aliases
  ([#72](https://github.com/UiPath/coder_eval/pull/72),
  [`8574ded`](https://github.com/UiPath/coder_eval/commit/8574dedb02d6e9ab883b042934f89297591e8abd))

- **early-stop**: Per-criterion arming via stop_early blocks on live criteria
  ([#78](https://github.com/UiPath/coder_eval/pull/78),
  [`4cf8092`](https://github.com/UiPath/coder_eval/commit/4cf80920422e25fec663ef93e483902bdeffad24))

### Refactoring

- **command-executed**: Total _match_haystacks, shared window, memoized
  ([#77](https://github.com/UiPath/coder_eval/pull/77),
  [`7abd080`](https://github.com/UiPath/coder_eval/commit/7abd08098146067890e16740c37239c2e0009a24))


## v0.9.4 (2026-08-04)

### Bug Fixes

- **litellm**: Pin litellm[proxy]==1.95.0 + fastapi==0.140.0 for proxy startup
  ([#76](https://github.com/UiPath/coder_eval/pull/76),
  [`f2f8580`](https://github.com/UiPath/coder_eval/commit/f2f85807cff3376b7201d5d5b2f9e1c4874220d9))

- **litellm**: Pin proxy deps (litellm 1.95.0 + fastapi 0.140.0) to fix startup crash
  ([#76](https://github.com/UiPath/coder_eval/pull/76),
  [`f2f8580`](https://github.com/UiPath/coder_eval/commit/f2f85807cff3376b7201d5d5b2f9e1c4874220d9))

### Chores

- **action**: Rename Marketplace listing to coder_eval, add author
  ([`a9c274d`](https://github.com/UiPath/coder_eval/commit/a9c274d918114df5229b20e9c65a4ce620b9f9ed))

### Documentation

- **litellm**: Surface the proxy dep-pin override vars in start script
  ([#76](https://github.com/UiPath/coder_eval/pull/76),
  [`f2f8580`](https://github.com/UiPath/coder_eval/commit/f2f85807cff3376b7201d5d5b2f9e1c4874220d9))

### Refactoring

- **litellm**: Address PR review — pin SSOT guard, rename, doc ripple
  ([#76](https://github.com/UiPath/coder_eval/pull/76),
  [`f2f8580`](https://github.com/UiPath/coder_eval/commit/f2f85807cff3376b7201d5d5b2f9e1c4874220d9))


## v0.9.3 (2026-08-04)

### Bug Fixes

- **early-stop**: Address PR review — polarity-blind budget, pass_threshold displacement,
  gate-semantic split ([#74](https://github.com/UiPath/coder_eval/pull/74),
  [`800ac77`](https://github.com/UiPath/coder_eval/commit/800ac7730e53a0ce9e9128b42ea91f0030d01f89))

### Chores

- **deps**: Bump aiohttp 3.14.1→3.14.3, cryptography 49.0.0→50.0.0
  ([#74](https://github.com/UiPath/coder_eval/pull/74),
  [`800ac77`](https://github.com/UiPath/coder_eval/commit/800ac7730e53a0ce9e9128b42ea91f0030d01f89))

### Features

- **early-stop**: Weighted ceiling/floor bounds + decision-step budget
  ([#74](https://github.com/UiPath/coder_eval/pull/74),
  [`800ac77`](https://github.com/UiPath/coder_eval/commit/800ac7730e53a0ce9e9128b42ea91f0030d01f89))


## v0.9.2 (2026-07-31)

### Bug Fixes

- **cost**: A task timeout with no preserved turn is unrecorded spend, not free
  ([#63](https://github.com/UiPath/coder_eval/pull/63),
  [`93c7fc0`](https://github.com/UiPath/coder_eval/commit/93c7fc086bbefe74e25625dd8b77106bef736c09))

- **cost**: Book spend on the error and timeout paths, flag what is unpriced
  ([#63](https://github.com/UiPath/coder_eval/pull/63),
  [`93c7fc0`](https://github.com/UiPath/coder_eval/commit/93c7fc086bbefe74e25625dd8b77106bef736c09))

- **cost**: Flag every hard-killed task as a cost floor, not just the empty ones
  ([#63](https://github.com/UiPath/coder_eval/pull/63),
  [`93c7fc0`](https://github.com/UiPath/coder_eval/commit/93c7fc086bbefe74e25625dd8b77106bef736c09))

- **evalboard**: Honest scoped counts, and one definition of a run's scope
  ([#69](https://github.com/UiPath/coder_eval/pull/69),
  [`0bdac0b`](https://github.com/UiPath/coder_eval/commit/0bdac0b2bab0d9f2937a2ddc5e54b26d110a56d1))

- **litellm**: Gate cost_log_tags on agent capability, not route (fixes non-Claude crash)
  ([#66](https://github.com/UiPath/coder_eval/pull/66),
  [`4131a2a`](https://github.com/UiPath/coder_eval/commit/4131a2a2983c347023927c66c04756595c3dbceb))

- **litellm**: Make the orphaned-spend warning actually fire
  ([#66](https://github.com/UiPath/coder_eval/pull/66),
  [`4131a2a`](https://github.com/UiPath/coder_eval/commit/4131a2a2983c347023927c66c04756595c3dbceb))

- **litellm**: Per-attempt cost-log scoping + single run-id accessor + no-match warning
  ([#66](https://github.com/UiPath/coder_eval/pull/66),
  [`4131a2a`](https://github.com/UiPath/coder_eval/commit/4131a2a2983c347023927c66c04756595c3dbceb))

- **litellm**: Pin each open-weight model to a vetted provider set (no silent fallback)
  ([#66](https://github.com/UiPath/coder_eval/pull/66),
  [`4131a2a`](https://github.com/UiPath/coder_eval/commit/4131a2a2983c347023927c66c04756595c3dbceb))

- **litellm**: Proxy-authoritative token buckets + all-priced gate + transactional join
  ([#66](https://github.com/UiPath/coder_eval/pull/66),
  [`4131a2a`](https://github.com/UiPath/coder_eval/commit/4131a2a2983c347023927c66c04756595c3dbceb))

- **litellm**: Sanitize cost headers, reject non-finite cost, drop debug scaffolding
  ([#66](https://github.com/UiPath/coder_eval/pull/66),
  [`4131a2a`](https://github.com/UiPath/coder_eval/commit/4131a2a2983c347023927c66c04756595c3dbceb))

- **orchestrator**: Recover the in-flight turn's spend on a hard kill
  ([#63](https://github.com/UiPath/coder_eval/pull/63),
  [`93c7fc0`](https://github.com/UiPath/coder_eval/commit/93c7fc086bbefe74e25625dd8b77106bef736c09))

- **pricing**: Add the claude-opus-5 rate so killed turns stop booking zero
  ([#63](https://github.com/UiPath/coder_eval/pull/63),
  [`93c7fc0`](https://github.com/UiPath/coder_eval/commit/93c7fc086bbefe74e25625dd8b77106bef736c09))

- **pricing**: Add the five unpriced codex tiers still on OpenAI's rate card
  ([#63](https://github.com/UiPath/coder_eval/pull/63),
  [`93c7fc0`](https://github.com/UiPath/coder_eval/commit/93c7fc086bbefe74e25625dd8b77106bef736c09))

- **pricing**: Correct every wrong rate-card entry and close the alias gaps
  ([#63](https://github.com/UiPath/coder_eval/pull/63),
  [`93c7fc0`](https://github.com/UiPath/coder_eval/commit/93c7fc086bbefe74e25625dd8b77106bef736c09))

- **pricing**: Refresh the rate card and correct gemini-3-flash-preview
  ([#63](https://github.com/UiPath/coder_eval/pull/63),
  [`93c7fc0`](https://github.com/UiPath/coder_eval/commit/93c7fc086bbefe74e25625dd8b77106bef736c09))

- **reports**: Count errors as misses and stop losing cost on error paths
  ([#63](https://github.com/UiPath/coder_eval/pull/63),
  [`93c7fc0`](https://github.com/UiPath/coder_eval/commit/93c7fc086bbefe74e25625dd8b77106bef736c09))

- **reports**: Count errors as misses in one canonical pass rate
  ([#63](https://github.com/UiPath/coder_eval/pull/63),
  [`93c7fc0`](https://github.com/UiPath/coder_eval/commit/93c7fc086bbefe74e25625dd8b77106bef736c09))

### Code Style

- **evalboard**: Drop the swatch dots and the scope caption from the header
  ([#69](https://github.com/UiPath/coder_eval/pull/69),
  [`0bdac0b`](https://github.com/UiPath/coder_eval/commit/0bdac0b2bab0d9f2937a2ddc5e54b26d110a56d1))

### Documentation

- **cost**: Describe the per-turn backfill as the net it is
  ([#63](https://github.com/UiPath/coder_eval/pull/63),
  [`93c7fc0`](https://github.com/UiPath/coder_eval/commit/93c7fc086bbefe74e25625dd8b77106bef736c09))

- **cost**: Describe the unpriced-crash mechanism accurately and keep comments framework-general
  ([#63](https://github.com/UiPath/coder_eval/pull/63),
  [`93c7fc0`](https://github.com/UiPath/coder_eval/commit/93c7fc086bbefe74e25625dd8b77106bef736c09))

- **litellm**: Correct the cost contract after cutting per-message distribution
  ([#66](https://github.com/UiPath/coder_eval/pull/66),
  [`4131a2a`](https://github.com/UiPath/coder_eval/commit/4131a2a2983c347023927c66c04756595c3dbceb))

- **litellm**: Document LITELLM_COST_LOG wiring + correct the reconciliation-cost contract
  ([#66](https://github.com/UiPath/coder_eval/pull/66),
  [`4131a2a`](https://github.com/UiPath/coder_eval/commit/4131a2a2983c347023927c66c04756595c3dbceb))

### Features

- **cost**: Publish one accurate total on every reporting surface
  ([#63](https://github.com/UiPath/coder_eval/pull/63),
  [`93c7fc0`](https://github.com/UiPath/coder_eval/commit/93c7fc086bbefe74e25625dd8b77106bef736c09))

- **docker**: Bind-mount the LiteLLM cost log so --driver docker joins actual cost
  ([#66](https://github.com/UiPath/coder_eval/pull/66),
  [`4131a2a`](https://github.com/UiPath/coder_eval/commit/4131a2a2983c347023927c66c04756595c3dbceb))

- **evalboard**: Compare every harness on the overview, and scope the whole page to one
  ([#69](https://github.com/UiPath/coder_eval/pull/69),
  [`0bdac0b`](https://github.com/UiPath/coder_eval/commit/0bdac0b2bab0d9f2937a2ddc5e54b26d110a56d1))

- **evalboard**: Compare harnesses on the overview, and identify each run
  ([#69](https://github.com/UiPath/coder_eval/pull/69),
  [`0bdac0b`](https://github.com/UiPath/coder_eval/commit/0bdac0b2bab0d9f2937a2ddc5e54b26d110a56d1))

- **evalboard**: Lift the harness scope to the page header, in vendor colors
  ([#69](https://github.com/UiPath/coder_eval/pull/69),
  [`0bdac0b`](https://github.com/UiPath/coder_eval/commit/0bdac0b2bab0d9f2937a2ddc5e54b26d110a56d1))

- **evalboard**: Make each turn's provider-call table a collapsed dropdown
  ([#66](https://github.com/UiPath/coder_eval/pull/66),
  [`4131a2a`](https://github.com/UiPath/coder_eval/commit/4131a2a2983c347023927c66c04756595c3dbceb))

- **evalboard**: Mark a partly-priced run total as a floor, not the bill
  ([#63](https://github.com/UiPath/coder_eval/pull/63),
  [`93c7fc0`](https://github.com/UiPath/coder_eval/commit/93c7fc086bbefe74e25625dd8b77106bef736c09))

- **evalboard**: One set of pass-rate cutoffs, and a run table that pages through all history
  ([#69](https://github.com/UiPath/coder_eval/pull/69),
  [`0bdac0b`](https://github.com/UiPath/coder_eval/commit/0bdac0b2bab0d9f2937a2ddc5e54b26d110a56d1))

- **evalboard**: Per-call cost/cache table from provider_call_costs (replaces inline)
  ([#66](https://github.com/UiPath/coder_eval/pull/66),
  [`4131a2a`](https://github.com/UiPath/coder_eval/commit/4131a2a2983c347023927c66c04756595c3dbceb))

- **evalboard**: Read the canonical pass rate and surface incomplete cost
  ([#63](https://github.com/UiPath/coder_eval/pull/63),
  [`93c7fc0`](https://github.com/UiPath/coder_eval/commit/93c7fc086bbefe74e25625dd8b77106bef736c09))

- **evalboard**: Say which harness, model, and framework version a run used
  ([#69](https://github.com/UiPath/coder_eval/pull/69),
  [`0bdac0b`](https://github.com/UiPath/coder_eval/commit/0bdac0b2bab0d9f2937a2ddc5e54b26d110a56d1))

- **litellm**: Actual per-call cost + cache accounting for the open-weight backend
  ([#66](https://github.com/UiPath/coder_eval/pull/66),
  [`4131a2a`](https://github.com/UiPath/coder_eval/commit/4131a2a2983c347023927c66c04756595c3dbceb))

### Refactoring

- **cost**: Correct the simulator-cost bound and drop the unread variant error share
  ([#63](https://github.com/UiPath/coder_eval/pull/63),
  [`93c7fc0`](https://github.com/UiPath/coder_eval/commit/93c7fc086bbefe74e25625dd8b77106bef736c09))

- **cost**: Cut the commentary and drop unreachable rate-card keys
  ([#63](https://github.com/UiPath/coder_eval/pull/63),
  [`93c7fc0`](https://github.com/UiPath/coder_eval/commit/93c7fc086bbefe74e25625dd8b77106bef736c09))

- **cost**: Define the unpriced-row test once, and only for new runs
  ([#63](https://github.com/UiPath/coder_eval/pull/63),
  [`93c7fc0`](https://github.com/UiPath/coder_eval/commit/93c7fc086bbefe74e25625dd8b77106bef736c09))

- **cost**: Total_cost_usd means the whole bill everywhere
  ([#63](https://github.com/UiPath/coder_eval/pull/63),
  [`93c7fc0`](https://github.com/UiPath/coder_eval/commit/93c7fc086bbefe74e25625dd8b77106bef736c09))

- **evalboard**: Call the UiPath harness Delegate
  ([#69](https://github.com/UiPath/coder_eval/pull/69),
  [`0bdac0b`](https://github.com/UiPath/coder_eval/commit/0bdac0b2bab0d9f2937a2ddc5e54b26d110a56d1))

- **litellm**: Cut per-message distribution; turn-level join + per-call audit record
  ([#66](https://github.com/UiPath/coder_eval/pull/66),
  [`4131a2a`](https://github.com/UiPath/coder_eval/commit/4131a2a2983c347023927c66c04756595c3dbceb))

- **litellm**: Drop the provider field/column — unavailable on the streaming path
  ([#66](https://github.com/UiPath/coder_eval/pull/66),
  [`4131a2a`](https://github.com/UiPath/coder_eval/commit/4131a2a2983c347023927c66c04756595c3dbceb))

- **litellm**: Stream the cost log + de-duplicate the OpenRouter config comment
  ([#66](https://github.com/UiPath/coder_eval/pull/66),
  [`4131a2a`](https://github.com/UiPath/coder_eval/commit/4131a2a2983c347023927c66c04756595c3dbceb))

### Testing

- **litellm**: Cover config shape, join ordering, and defensive cost branches
  ([#66](https://github.com/UiPath/coder_eval/pull/66),
  [`4131a2a`](https://github.com/UiPath/coder_eval/commit/4131a2a2983c347023927c66c04756595c3dbceb))


## v0.9.1 (2026-07-29)

### Features

- **agents**: Extend cooperative early stop to codex and antigravity
  ([`b849421`](https://github.com/UiPath/coder_eval/commit/b8494218af6870feff2a9809e3050e96da849cd8))


## v0.9.0 (2026-07-28)

### Features

- **criteria**: Async-primary BaseCriterion contract with CheckerMisuseError escalation
  ([#60](https://github.com/UiPath/coder_eval/pull/60))

## v0.8.10 (2026-07-24)

### Bug Fixes

- Remove dead SimulationConfig.parallel_trials; add CE031 to guard the class
  ([`947acd3`](https://github.com/UiPath/coder_eval/commit/947acd34a4f0451c69cb7bf0eb863127b9995f76))

- **early-stop**: Add stop_when 'auto' — per-instance arming + pass-armed-subset stop rule
  ([#51](https://github.com/UiPath/coder_eval/pull/51),
  [`08e21e6`](https://github.com/UiPath/coder_eval/commit/08e21e67b67fdab84d26b8a0e5551e04289d92a8))

- **early-stop**: Defer fail-stop while a pass-armed criterion is undecided
  ([#51](https://github.com/UiPath/coder_eval/pull/51),
  [`08e21e6`](https://github.com/UiPath/coder_eval/commit/08e21e67b67fdab84d26b8a0e5551e04289d92a8))

- **early-stop**: Return assert_never explicitly to satisfy CodeQL
  ([#51](https://github.com/UiPath/coder_eval/pull/51),
  [`08e21e6`](https://github.com/UiPath/coder_eval/commit/08e21e67b67fdab84d26b8a0e5551e04289d92a8))

- **reports**: Code review fixes for the JUnit CI gate
  ([#37](https://github.com/UiPath/coder_eval/pull/37),
  [`74db6fa`](https://github.com/UiPath/coder_eval/commit/74db6facfea6f898af4db6709e563d90af0d7b30))

- **reports**: JUnit CI-gate review fixes + CE027 env-var lint
  ([#37](https://github.com/UiPath/coder_eval/pull/37),
  [`74db6fa`](https://github.com/UiPath/coder_eval/commit/74db6facfea6f898af4db6709e563d90af0d7b30))

- **reports**: Make skipped-task JUnit names platform-independent
  ([#37](https://github.com/UiPath/coder_eval/pull/37),
  [`74db6fa`](https://github.com/UiPath/coder_eval/commit/74db6facfea6f898af4db6709e563d90af0d7b30))

### Chores

- Re-trigger CI (GitHub dropped the force-push event)
  ([#37](https://github.com/UiPath/coder_eval/pull/37),
  [`74db6fa`](https://github.com/UiPath/coder_eval/commit/74db6facfea6f898af4db6709e563d90af0d7b30))

- **deps**: Lock defusedxml (dev-only, test-side XML parsing)
  ([#37](https://github.com/UiPath/coder_eval/pull/37),
  [`74db6fa`](https://github.com/UiPath/coder_eval/commit/74db6facfea6f898af4db6709e563d90af0d7b30))

### Continuous Integration

- Disable Docs gh-pages auto-publish on push (Pages not enabled yet)
  ([`c289d46`](https://github.com/UiPath/coder_eval/commit/c289d46c5cec269f205224e4dfce27affab5557e))

### Documentation

- 1/8 — add DATASETS.md and a task-schema dataset: section
  ([`e3d37ac`](https://github.com/UiPath/coder_eval/commit/e3d37ac793e6f12c020b71a4ed19df2279b4b2c1))

- 2/8 — retire BYOD.md into DOCKER_ISOLATION.md
  ([`1a4a2a5`](https://github.com/UiPath/coder_eval/commit/1a4a2a5893639d14832dbd1614163aa7c18bf70a))

- 3/8 — one complete run_limits reference; document skip
  ([`bcb7e24`](https://github.com/UiPath/coder_eval/commit/bcb7e24132c229114efe9a38281a37e736d83312))

- 4/8 — add DIALOG_MODE.md and correct four stale simulation claims
  ([`18dcbe9`](https://github.com/UiPath/coder_eval/commit/18dcbe95d156856149538406ededc8c75ec594f8))

- 5/8 — fix prompt_mutations example; add CE029
  ([`b524009`](https://github.com/UiPath/coder_eval/commit/b5240099c58f5b50c7b5fe531a12f1f8b98aec29))

- 7/8 — generate flat indexes from the mkdocs nav; add CE028
  ([`1514bcb`](https://github.com/UiPath/coder_eval/commit/1514bcb8f075ed48f2857a84269513c60878ea81))

- Add CI Gate reference (GitHub Action + JUnit) and wire into indexes
  ([`b8c6301`](https://github.com/UiPath/coder_eval/commit/b8c63018bd93ba91751511d95f4af96a444d9873))

- Agent guides, extending & report-schema references, and fixes
  ([`ce74824`](https://github.com/UiPath/coder_eval/commit/ce74824bde2b5322dc745bace045e1806f77324b))

- Fold nav long tail into one Advanced group; align index ordering
  ([`e2ff053`](https://github.com/UiPath/coder_eval/commit/e2ff0539af8c9e3e6deb85c0a0124c0232224033))

- Point docs links to coder-eval.com/docs; drop Ruff badge
  ([`60430e5`](https://github.com/UiPath/coder_eval/commit/60430e5e857f0af6fbf08cde00f0621de4610562))

- Point pyproject Documentation URL to coder-eval.com/docs
  ([`be39df2`](https://github.com/UiPath/coder_eval/commit/be39df23c8ba1a0f6f945dc99e3827eb5d6f2772))

- Reword CODER_EVAL_RAW_SDK_LOG to prose form (satisfy CE027)
  ([`d7d5b59`](https://github.com/UiPath/coder_eval/commit/d7d5b59b990127810fcfe33272f3ff733a9498cd))

- Use the brand name "Coder Eval" in prose and titles
  ([`821f11b`](https://github.com/UiPath/coder_eval/commit/821f11bf94762f6578913090568f5b092e237596))

### Features

- Packaged CI gate — JUnit XML output + composite GitHub Action
  ([#37](https://github.com/UiPath/coder_eval/pull/37),
  [`74db6fa`](https://github.com/UiPath/coder_eval/commit/74db6facfea6f898af4db6709e563d90af0d7b30))

- **action**: Generic env passthrough + minimum-task-score gate
  ([#37](https://github.com/UiPath/coder_eval/pull/37),
  [`74db6fa`](https://github.com/UiPath/coder_eval/commit/74db6facfea6f898af4db6709e563d90af0d7b30))

- **ci**: 3/3 — publish composite action, release automation, PR dogfood
  ([#37](https://github.com/UiPath/coder_eval/pull/37),
  [`74db6fa`](https://github.com/UiPath/coder_eval/commit/74db6facfea6f898af4db6709e563d90af0d7b30))

- **cli**: 2/3 — wire run --junit-xml and report -f junit
  ([#37](https://github.com/UiPath/coder_eval/pull/37),
  [`74db6fa`](https://github.com/UiPath/coder_eval/commit/74db6facfea6f898af4db6709e563d90af0d7b30))

- **reports**: 1/3 — add reports_junit.py disk-driven JUnit XML writer
  ([#37](https://github.com/UiPath/coder_eval/pull/37),
  [`74db6fa`](https://github.com/UiPath/coder_eval/commit/74db6facfea6f898af4db6709e563d90af0d7b30))

### Testing

- 6/8 — CE030 documents-or-exempts model fields
  ([`c9a3b16`](https://github.com/UiPath/coder_eval/commit/c9a3b160deeec385386a7009812025bc26021363))


## v0.8.9 (2026-07-23)

### Bug Fixes

- Code review fixes for welch-t-test-exact ([#38](https://github.com/UiPath/coder_eval/pull/38),
  [`6df6e9b`](https://github.com/UiPath/coder_eval/commit/6df6e9bb1f3bd009592185ec3473e8f2e67d3816))

- Render weight:0 criteria as informational on every display surface
  ([#34](https://github.com/UiPath/coder_eval/pull/34),
  [`9a34e90`](https://github.com/UiPath/coder_eval/commit/9a34e90bba2854316149cad61613a75dd3bd91e4))

- Weight:0 un-gates criteria (informational criteria)
  ([#34](https://github.com/UiPath/coder_eval/pull/34),
  [`9a34e90`](https://github.com/UiPath/coder_eval/commit/9a34e90bba2854316149cad61613a75dd3bd91e4))

- Weight:0 un-gates criteria and renders as informational
  ([#34](https://github.com/UiPath/coder_eval/pull/34),
  [`9a34e90`](https://github.com/UiPath/coder_eval/commit/9a34e90bba2854316149cad61613a75dd3bd91e4))

- **early-stop**: Decide skill activation on the tool call, not its result
  ([#43](https://github.com/UiPath/coder_eval/pull/43),
  [`d34aa97`](https://github.com/UiPath/coder_eval/commit/d34aa97416ae3af09d09cdf78cb6607b4e2e6f7c))

- **early-stop**: Latch skill activation on any engagement, not first
  ([#43](https://github.com/UiPath/coder_eval/pull/43),
  [`d34aa97`](https://github.com/UiPath/coder_eval/commit/d34aa97416ae3af09d09cdf78cb6607b4e2e6f7c))

- **evalboard**: Match watchlist skeleton header to avoid layout shift
  ([#45](https://github.com/UiPath/coder_eval/pull/45),
  [`acc1c86`](https://github.com/UiPath/coder_eval/commit/acc1c86bc97b80383ce1ef402356853447088e4a))

- **reports**: 1/2 — exact Student-t p-values in welch_t_test
  ([#38](https://github.com/UiPath/coder_eval/pull/38),
  [`6df6e9b`](https://github.com/UiPath/coder_eval/commit/6df6e9bb1f3bd009592185ec3473e8f2e67d3816))

- **reports**: Exact Student-t p-values and a paired comparison section
  ([#38](https://github.com/UiPath/coder_eval/pull/38),
  [`6df6e9b`](https://github.com/UiPath/coder_eval/commit/6df6e9bb1f3bd009592185ec3473e8f2e67d3816))

- **reports**: Fail loud on t* overflow; surface excluded paired tasks
  ([#38](https://github.com/UiPath/coder_eval/pull/38),
  [`6df6e9b`](https://github.com/UiPath/coder_eval/commit/6df6e9bb1f3bd009592185ec3473e8f2e67d3816))

- **reports**: One source of truth for variant series and paired stats
  ([#38](https://github.com/UiPath/coder_eval/pull/38),
  [`6df6e9b`](https://github.com/UiPath/coder_eval/commit/6df6e9bb1f3bd009592185ec3473e8f2e67d3816))

- **reports**: Validate confidence and n_resamples in bootstrap_mean_ci
  ([#38](https://github.com/UiPath/coder_eval/pull/38),
  [`6df6e9b`](https://github.com/UiPath/coder_eval/commit/6df6e9bb1f3bd009592185ec3473e8f2e67d3816))

### Chores

- **harness**: Defer two guards from the welch-t-test-exact run
  ([#38](https://github.com/UiPath/coder_eval/pull/38),
  [`6df6e9b`](https://github.com/UiPath/coder_eval/commit/6df6e9bb1f3bd009592185ec3473e8f2e67d3816))

### Documentation

- Add adopter issue template and ADOPTERS.md ([#40](https://github.com/UiPath/coder_eval/pull/40),
  [`bfbed4e`](https://github.com/UiPath/coder_eval/commit/bfbed4e4a2ca857167fac2cb15462b7159e97684))

- Switch multi-model review from codex to gpt-5 alias
  ([#36](https://github.com/UiPath/coder_eval/pull/36),
  [`0a3f2a7`](https://github.com/UiPath/coder_eval/commit/0a3f2a71a0a1e1e56f42a668fff57bac06eb99ec))

### Features

- **evalboard**: Make all pages harness-aware and stream tables
  ([#45](https://github.com/UiPath/coder_eval/pull/45),
  [`acc1c86`](https://github.com/UiPath/coder_eval/commit/acc1c86bc97b80383ce1ef402356853447088e4a))

- **evalboard**: Make analytics surfaces harness-aware and stream tables
  ([#45](https://github.com/UiPath/coder_eval/pull/45),
  [`acc1c86`](https://github.com/UiPath/coder_eval/commit/acc1c86bc97b80383ce1ef402356853447088e4a))

- **evalboard**: Scope task trends to one harness
  ([#45](https://github.com/UiPath/coder_eval/pull/45),
  [`acc1c86`](https://github.com/UiPath/coder_eval/commit/acc1c86bc97b80383ce1ef402356853447088e4a))

- **reports**: 2/2 — add a Paired Comparison section to experiment reports
  ([#38](https://github.com/UiPath/coder_eval/pull/38),
  [`6df6e9b`](https://github.com/UiPath/coder_eval/commit/6df6e9bb1f3bd009592185ec3473e8f2e67d3816))

### Refactoring

- **evalboard**: Address review nits on harness plumbing
  ([#45](https://github.com/UiPath/coder_eval/pull/45),
  [`acc1c86`](https://github.com/UiPath/coder_eval/commit/acc1c86bc97b80383ce1ef402356853447088e4a))

### Testing

- **early-stop**: Cover second-review items (two-AgentStart, golden corpus, parity)
  ([#43](https://github.com/UiPath/coder_eval/pull/43),
  [`d34aa97`](https://github.com/UiPath/coder_eval/commit/d34aa97416ae3af09d09cdf78cb6607b4e2e6f7c))


## v0.8.8 (2026-07-22)

### Bug Fixes

- **codex**: Create CODEX_HOME before pinning it in the app-server env
  ([#39](https://github.com/UiPath/coder_eval/pull/39),
  [`8d98b91`](https://github.com/UiPath/coder_eval/commit/8d98b912ae4767904c7d1c395913c3d0aaec6c66))

### Documentation

- Add Website badge and point PyPI Homepage at coder-eval.com
  ([#41](https://github.com/UiPath/coder_eval/pull/41),
  [`0551534`](https://github.com/UiPath/coder_eval/commit/055153409d86b8d537bba73ef7ed58dfd17fee26))


## v0.8.7 (2026-07-22)

### Bug Fixes

- Detect Windows skill paths in telemetry ([#24](https://github.com/UiPath/coder_eval/pull/24),
  [`c57a6b0`](https://github.com/UiPath/coder_eval/commit/c57a6b0408ef116177b15bc9b09a768096aaa33f))

- **agents**: Run codex + antigravity harnesses on the tempdir/host path
  ([#33](https://github.com/UiPath/coder_eval/pull/33),
  [`04498a9`](https://github.com/UiPath/coder_eval/commit/04498a96f6b9ac0cc5c73f36e73a361f3f3fa0ae))

- **antigravity**: Pin google-antigravity 0.1.7 to load on glibc 2.35
  ([#33](https://github.com/UiPath/coder_eval/pull/33),
  [`04498a9`](https://github.com/UiPath/coder_eval/commit/04498a96f6b9ac0cc5c73f36e73a361f3f3fa0ae))

- **codex**: Always run full-access; drop the in-process OS sandbox
  ([#33](https://github.com/UiPath/coder_eval/pull/33),
  [`04498a9`](https://github.com/UiPath/coder_eval/commit/04498a96f6b9ac0cc5c73f36e73a361f3f3fa0ae))

- **codex**: Cover zsh login shells (macOS default) in the mock-PATH home shim
  ([#26](https://github.com/UiPath/coder_eval/pull/26),
  [`73f0db2`](https://github.com/UiPath/coder_eval/commit/73f0db24df0b67f82d46e3bf15c85a21837237ef))

- **codex**: Keep mock CLIs shadowed in bash login shells
  ([#26](https://github.com/UiPath/coder_eval/pull/26),
  [`73f0db2`](https://github.com/UiPath/coder_eval/commit/73f0db24df0b67f82d46e3bf15c85a21837237ef))

- **codex**: Make login-shell temp-home lifecycle exception-safe incl. kill_sync
  ([#26](https://github.com/UiPath/coder_eval/pull/26),
  [`73f0db2`](https://github.com/UiPath/coder_eval/commit/73f0db24df0b67f82d46e3bf15c85a21837237ef))

- **codex**: Restore mock-CLI PATH prepend in login shells via per-task HOME profile
  ([#26](https://github.com/UiPath/coder_eval/pull/26),
  [`73f0db2`](https://github.com/UiPath/coder_eval/commit/73f0db24df0b67f82d46e3bf15c85a21837237ef))

- **codex**: Restore original HOME inside generated login-shell profile
  ([#26](https://github.com/UiPath/coder_eval/pull/26),
  [`73f0db2`](https://github.com/UiPath/coder_eval/commit/73f0db24df0b67f82d46e3bf15c85a21837237ef))

- **codex**: Use full-access sandbox on coder_eval-managed tempdir path
  ([#33](https://github.com/UiPath/coder_eval/pull/33),
  [`04498a9`](https://github.com/UiPath/coder_eval/commit/04498a96f6b9ac0cc5c73f36e73a361f3f3fa0ae))

- **deps**: Bump pyasn1 to 0.6.4 for GHSA-8ppf-4f7h-5ppj / GHSA-hm4w-wwcw-mr6r
  ([#33](https://github.com/UiPath/coder_eval/pull/33),
  [`04498a9`](https://github.com/UiPath/coder_eval/commit/04498a96f6b9ac0cc5c73f36e73a361f3f3fa0ae))

### Chores

- **deps**: Bump pyasn1 0.6.3 -> 0.6.4 (GHSA-8ppf-4f7h-5ppj, GHSA-hm4w-wwcw-mr6r)
  ([#32](https://github.com/UiPath/coder_eval/pull/32),
  [`f1f4f9f`](https://github.com/UiPath/coder_eval/commit/f1f4f9fba2bd034a271e50fc1a95bcc74c2ac19b))

- **deps**: Upgrade agent SDKs to latest (claude 0.2.124, codex 0.144.4)
  ([#33](https://github.com/UiPath/coder_eval/pull/33),
  [`04498a9`](https://github.com/UiPath/coder_eval/commit/04498a96f6b9ac0cc5c73f36e73a361f3f3fa0ae))

### Continuous Integration

- **release**: Publish a prerelease from a non-main branch
  ([#33](https://github.com/UiPath/coder_eval/pull/33),
  [`04498a9`](https://github.com/UiPath/coder_eval/commit/04498a96f6b9ac0cc5c73f36e73a361f3f3fa0ae))

### Documentation

- Add MkDocs docs site, comparison page, and SEO metadata
  ([#32](https://github.com/UiPath/coder_eval/pull/32),
  [`f1f4f9f`](https://github.com/UiPath/coder_eval/commit/f1f4f9fba2bd034a271e50fc1a95bcc74c2ac19b))

- Address PR review — Coder Eval naming, cleaner table, gh-deploy workflow
  ([#32](https://github.com/UiPath/coder_eval/pull/32),
  [`f1f4f9f`](https://github.com/UiPath/coder_eval/commit/f1f4f9fba2bd034a271e50fc1a95bcc74c2ac19b))

- Address review — git identity for gh-pages, single strict deploy, table glyphs, uv tool install in
  Tutorial 01 ([#32](https://github.com/UiPath/coder_eval/pull/32),
  [`f1f4f9f`](https://github.com/UiPath/coder_eval/commit/f1f4f9fba2bd034a271e50fc1a95bcc74c2ac19b))

### Refactoring

- **codex**: Drop dead sandbox branch + honest full-access messaging
  ([#33](https://github.com/UiPath/coder_eval/pull/33),
  [`04498a9`](https://github.com/UiPath/coder_eval/commit/04498a96f6b9ac0cc5c73f36e73a361f3f3fa0ae))

### Testing

- Accept sandbox_managed kwarg in agent test doubles
  ([#33](https://github.com/UiPath/coder_eval/pull/33),
  [`04498a9`](https://github.com/UiPath/coder_eval/commit/04498a96f6b9ac0cc5c73f36e73a361f3f3fa0ae))

- **codex**: Pin start-to-CodexConfig env composition and tidy test imports
  ([#26](https://github.com/UiPath/coder_eval/pull/26),
  [`73f0db2`](https://github.com/UiPath/coder_eval/commit/73f0db24df0b67f82d46e3bf15c85a21837237ef))


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
