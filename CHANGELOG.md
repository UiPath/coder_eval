# CHANGELOG

<!-- version list -->

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
