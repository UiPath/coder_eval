# LiteLLM proxy — coder_eval `litellm` (open-weight) backend

coder_eval evaluates UiPath skills on **non-Claude open-weight models** by driving
them through the **Claude Code SDK** harness (so the native `Skill` tool +
progressive disclosure fire unchanged). The SDK only speaks the **Anthropic
Messages** format (`POST /v1/messages`), but the target models don't:

| Target | Wire format | Needs translation? |
|---|---|---|
| Bedrock open-weight (GLM, DeepSeek, Kimi) | Converse | yes |
| OpenRouter (any model) | OpenAI Chat Completions | yes |

**LiteLLM is the translation shim.** The SDK is pointed at LiteLLM via
`ANTHROPIC_BASE_URL`; LiteLLM translates Anthropic ↔ Converse / OpenAI per model
and forwards to the real backend.

```
Claude Code SDK ──Anthropic /v1/messages──▶ LiteLLM (localhost:4000) ──▶ Bedrock Converse (eu-north-1)
  ANTHROPIC_BASE_URL=localhost:4000                                   └──▶ OpenRouter (OpenAI format)
```

Files:
- `litellm/litellm-config.yaml` — the model list + settings (this is the file you edit to add models).
- `litellm/start-litellm.sh` — launches the proxy, reading credentials out of `.env`.

---

## When to run `start-litellm.sh`

Run the proxy **whenever you run coder_eval against the `litellm` backend** — i.e.
`API_BACKEND=litellm` in `.env`, or `coder-eval run ... --backend litellm`. coder_eval
does **not** own the proxy lifecycle: it expects an already-running proxy at
`LITELLM_BASE_URL` and **fails fast at startup** if it isn't reachable
(`LiteLLM proxy not reachable at ... — Start it (e.g. litellm/start-litellm.sh)`).

You do **not** need it for the `direct` (Anthropic) or `bedrock` (Claude-on-Bedrock)
backends — those talk to their APIs directly.

### Prerequisites

`start-litellm.sh` reads these from `.env` and exports them into the proxy's own
environment before launching:

| Var | For | Notes |
|---|---|---|
| `AWS_BEARER_TOKEN_BEDROCK` | Bedrock models | required if you use any `bedrock/*` model |
| `AWS_REGION` | Bedrock models | defaults to `eu-north-1` |
| `OPENROUTER_API_KEY` | OpenRouter models | required if you use any `openrouter/*` model |
| `LITELLM_AUTH_TOKEN` | the virtual key clients present | becomes the proxy's `LITELLM_MASTER_KEY`; falls back to `sk-spike-local`. It can be customly designed.|

### Start it

```bash
bash litellm/start-litellm.sh          # foreground on :4000, Ctrl-C to stop
```

Overridable via env: `LITELLM_PORT` (default 4000), `LITELLM_CONFIG`, `ENV_FILE`,
`LITELLM_MASTER_KEY`, and the proxy dep pins `LITELLM_SPEC` / `LITELLM_FASTAPI_SPEC`
(full pip specifiers, e.g. `litellm[proxy]==1.95.0` / `fastapi==0.140.0` — set both
together when bumping).

### Point coder_eval at it

In `.env`:
```
API_BACKEND=litellm
LITELLM_BASE_URL=http://localhost:4000
LITELLM_AUTH_TOKEN=<same value start-litellm.sh printed as "master key">
LITELLM_MODEL=deepseek/deepseek-v4-pro   # default model when a task doesn't pin one
```
Then run — `--model` overrides the default:
```bash
coder-eval run tasks/<task>.yaml --backend litellm --model deepseek/deepseek-v4-pro
```

### Verify the proxy

```bash
KEY=$(grep -E '^LITELLM_AUTH_TOKEN=' .env | head -1 | sed -E 's/^LITELLM_AUTH_TOKEN=//; s/^"//; s/"$//')
# health
curl -s http://localhost:4000/health/liveliness
# which models are loaded
curl -s http://localhost:4000/v1/models -H "x-api-key: $KEY" \
  | python3 -c "import sys,json; print([m['id'] for m in json.load(sys.stdin)['data']])"
```

---

## Config file: `litellm-config.yaml`

```yaml
model_list:
  - model_name: <what clients pass as --model>   # also the pricing-table key
    litellm_params:
      model: <litellm provider-routed target>    # e.g. bedrock/converse/... or openrouter/...
      # ...provider-specific params

litellm_settings:
  drop_params: true          # silently drop provider-unsupported params instead of 400

general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY   # never hardcode a secret here
```

- **`model_name`** is the id clients (and `--model`) use, **and** the key coder_eval
  prices on. Keep it identical to the `pricing.py` key.
- **`model`** is LiteLLM's routing target — the `provider/...` prefix selects the
  translator + backend.

---

## Adding a new model

### 1. Add an entry to `model_list`

**Bedrock open-weight** (Converse, eu-north-1):
```yaml
  - model_name: some.bedrock-model
    litellm_params:
      model: bedrock/converse/some.bedrock-model
      aws_region_name: eu-north-1
```

**OpenRouter** (OpenAI format — pin the provider, see caveat below):
```yaml
  - model_name: vendor/some-model
    litellm_params:
      model: openrouter/vendor/some-model
      api_key: os.environ/OPENROUTER_API_KEY
      extra_body:
        provider:
          sort: price            # route to the cheapest upstream
          allow_fallbacks: false # never silently upgrade to a pricier provider
```
Find the exact OpenRouter slug + rates:
```bash
KEY=$(grep -E '^OPENROUTER_API_KEY=' .env | head -1 | sed -E 's/^OPENROUTER_API_KEY=//; s/^"//; s/"$//')
curl -s https://openrouter.ai/api/v1/models -H "Authorization: Bearer $KEY" \
  | python3 -c "import sys,json;[print(m['id'],m['pricing']) for m in json.load(sys.stdin)['data'] if 'SEARCH' in m['id'].lower()]"
```

### 2. Register pricing in **both** tables (rates must match)

- `src/coder_eval/pricing.py` — add to the `_PRICING` dict, keyed on the
  `model_name` (bare id as passed in `agent.model`):
  ```python
  "vendor/some-model": ModelPricing(input, output, cache_write, cache_read),
  ```
  These implicit-caching providers charge no separate cache-write fee, so
  `cache_write == input` (unused) and `cache_read` is the discounted rate.
- `evalboard/lib/pricing.ts` — add the same entry so the evalboard cost columns
  populate. A test (`lib/__tests__/pricing-parity.test.ts`) fails the build if a
  rate here disagrees with `pricing.py`.

### 3. Restart the proxy

LiteLLM reads `model_list` **once at startup**. After editing the yaml, `Ctrl-C`
and re-run `start-litellm.sh` — otherwise you get
`Invalid model name passed in model=...`.

### 4. Verify

`curl .../v1/models` should list the new `model_name`, then run a task with
`--model vendor/some-model`.

---

## ⚠️ OpenRouter cost accuracy (provider routing)

OpenRouter is a **multi-provider router**: the rate on a model's page is the
*cheapest* provider's headline, but a request can be routed to a pricier upstream
(observed: DeepSeek V4 Pro → Novita at ~$1.47/M input vs the $0.435/M headline,
3.4×). Because coder_eval prices `litellm`-backend runs from the **static**
`pricing.py` table, an un-pinned model can bill very differently from what
coder_eval reports.

**Mitigation:** always pin providers (`extra_body.provider: {sort: price,
allow_fallbacks: false}`), which makes the billed rate deterministic and equal to
the headline the pricing table uses. To confirm the actual provider/cost for a
run, read the OpenRouter **Activity** page (`provider_name`, `usage`) or query
`GET /api/v1/models/<id>/endpoints` for per-provider rates.

Token *counts* are exact and provider-independent; only the per-token *rate* is
subject to this. Bedrock models are single-provider and not affected.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `LiteLLM proxy not reachable at ...` (coder_eval startup) | Proxy not running — start it, or unset `LITELLM_BASE_URL`. |
| `Invalid model name passed in model=...` | Model added to yaml but proxy not restarted — restart it. |
| HTTP 401 / "Unable to locate credentials" | Missing `AWS_BEARER_TOKEN_BEDROCK` / `OPENROUTER_API_KEY` in `.env`, or key mismatch between `LITELLM_AUTH_TOKEN` (client) and the proxy's master key. |
| `ModuleNotFoundError: No module named 'proxy_server'` (masked startup death) | fastapi drifted past 0.140.0 (`get_flat_dependant` removed). Use `start-litellm.sh` (it pins the deps), or run with `--with 'fastapi==0.140.0'`. If overriding `LITELLM_SPEC`, bump `LITELLM_FASTAPI_SPEC` to match. |
| evalboard cost column blank for a model | Model missing from `evalboard/lib/pricing.ts`. |
