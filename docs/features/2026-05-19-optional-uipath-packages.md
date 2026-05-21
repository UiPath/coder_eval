# Optional `[uipath]` extra

**Date:** 2026-05-19
**Audience:** users / operators of `coder-eval`

The `uipath` and `uipath-llmgw-client` Python packages — both fetched from
UiPath's private package index — are no longer required runtime
dependencies. They moved into the optional `[uipath]` extra so a base
install works on hosts without private-index credentials.

## How to install

```bash
pip install coder-eval                  # core framework only
pip install 'coder-eval[uipath]'        # + UiPath features
pip install 'coder-eval[dev]'           # developer tooling
pip install 'coder-eval[dev,uipath]'    # everything (recommended for repo developers)
```

Or with uv:

```bash
uv sync --extra dev --extra uipath
```

## Which features need the extra?

| Feature                                                            | Needs `[uipath]`? |
| ------------------------------------------------------------------ | ----------------- |
| Agent loop, sandbox, all non-LLM criteria                          | no                |
| `llm_judge` via Anthropic SDK (`ANTHROPIC_API_KEY`)                | no                |
| `llm_judge` via AWS Bedrock                                        | no                |
| `llm_judge` via LLM Gateway transport (`judge_transport="llmgw"`)  | **yes**           |
| LLM Gateway proxy backend (`api_backend=proxy`, talks HTTP)        | no                |
| Prompt `rephrase` mutation                                         | **yes**           |
| `uipath_eval` criterion (uses the `uipath` CLI **in the sandbox**) | sandbox-side only |

## Where in the evaluation flow

- **At startup** — `models.routing._resolve_direct_judge_transport` checks
  whether `uipath_llmgw_client` is importable. If LLMGW credentials are set
  but the package is missing, it picks `judge_transport=None` instead of
  `"llmgw"`. The choice is recorded in `EvaluationResult.environment_info`.
- **At dispatch** — `llm_judge` with `judge_transport=None` returns a typed
  `JudgeCriterionResult` whose `error` field points at
  `pip install 'coder-eval[uipath]'`. The run is not aborted.
- **In the `rephrase` mutation** — the first invocation raises a
  `RuntimeError` with the same install hint if the package is missing.

## What error you see when you forget

```
RuntimeError: uipath_llmgw_client is required.
  Install with: pip install 'coder-eval[uipath]'
  (or `uv sync --extra uipath` for a dev checkout)
```

## CI surface

- The default `Quality Gate` CI job installs `.[dev,uipath]` and exercises
  the full system (unchanged from prior behaviour).
- A new `No-Extra Install (uipath optional)` CI job installs `.[dev]`
  without `UV_EXTRA_INDEX_URL` set, asserts the UiPath packages are
  absent, runs `init_criteria(validate=True)`, and runs the
  `tests/test_optional_dependencies.py` suite. This proves the contract
  on every PR.

The equivalent local check is `make verify-noextra`.
