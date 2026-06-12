# CHANGELOG

<!-- version list -->

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
