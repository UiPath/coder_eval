# CHANGELOG

<!-- version list -->

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
