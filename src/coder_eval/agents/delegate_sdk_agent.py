"""Delegate SDK agent — connects coder_eval to UiPath Autopilot's Delegate SDK.

We drive the agent through the published ``@uipath/delegate-stdio`` npm
package — the Delegate agent exposed as a runnable subprocess speaking a
newline-delimited JSON protocol on stdin/stdout (referred to as "the host"
below). The package is self-contained: ``@uipath/delegate-sdk`` is its npm
dependency and installs transitively, so installing it is the COMPLETE
install — there is no separate SDK to set up.

Prerequisites (runtime, documented — not enforced by coder_eval):

* Install the host package (``npm install @uipath/delegate-stdio``, from the
  public npm registry; the ``@uipath/delegate-sdk`` it pulls in is published
  there too), then point coder_eval at it via either:
    - ``DELEGATE_STDIO_PATH`` → absolute path to the bundle
      (``.../@uipath/delegate-stdio/dist/delegate_stdio.mjs``), or
    - ``DELEGATE_STDIO_NODE_MODULES`` → directory whose ``node_modules`` holds the
      installed package.
  When neither is set the bundle is auto-located by walking up from the cwd
  through its ancestors (and ``~``), the way Node resolves modules — so an
  ``npm install`` in the launch directory *or any ancestor* is found with zero
  configuration (npm with no local ``package.json`` silently installs into the
  nearest ancestor that has one, often ``~``).
* ``DELEGATE_SDK_ENV`` env var → cloud env slug (``alpha`` / ``staging`` /
  ``production``); defaults to ``alpha`` when unset or blank. The host composes
  the backend URL from the saved auth's org/tenant slugs plus this slug, and the
  Delegate SDK spawns its own interop — so there are normally no backend or
  interop URLs to configure.
* Advanced/local override only: ``BACKEND_URL`` / ``INTEROP_URL`` pin the backend
  / interop endpoints directly (e.g. a localhost backend on
  ``http://localhost:5002``). Host precedence: ``backendUrl`` > ``BACKEND_URL``
  env > ``env`` slug > localhost.
* ``DELEGATE_STALL_TIMEOUT_S`` env var (opt-in) → seconds to wait for the host's
  first agent activity before treating a silent backend round-trip as a
  transient stall and respawning+resending. Unset (default) disables it, so
  callers pointing at a healthy dedicated backend are unaffected. See
  :func:`_resolve_stall_timeout`.
* ``DELEGATE_STDIO_VERBOSE`` env var → trace the host's stdio protocol (every
  frame it writes, every agent event, its auth/init/token-refresh steps) into
  the run log. ``1``/``true`` forces it on, ``0``/``false`` forces it off, and
  unset follows coder_eval's own verbosity (``--verbose``). Set it when a turn
  looks hung: without it the host is silent for the whole round-trip, so a
  long turn is indistinguishable from a wedged one. Companion knob
  ``DELEGATE_STDIO_LOG_MAX_CHARS`` (host-side, default 50 000) caps each traced
  value. See :func:`_resolve_stdio_verbose`.
* Auth: either ``AUTH_TOKEN``/``TENANT_ID``/``ORG_ID`` env vars (plus ``USER_ID``
  for S2S tokens — all consumed by the host process, never read by coder_eval
  itself), or a previous ``delegate-cli login`` that wrote
  ``~/.aria/sdk-auth.json``. For runs longer than the token's ~1h TTL, set
  ``DELEGATE_AUTH_TOKEN_FILE`` to the token file an external process keeps
  fresh: the host re-reads it at every turn and before each TTL boundary and
  applies the newer token live, so a run outlives its start-up token without
  this adapter touching credentials. It accepts a ``PATH``-style list and takes
  the first readable entry, which is how one value covers both a host-run task
  and a docker task that sees the same file at its bind-mount path. When no
  such file is configured but ``AUTH_TOKEN`` was minted from the run's own
  ``LLMGW_*`` client-credentials pair, the adapter maintains the token file
  itself (re-minting adapter-side before each expiry) — see
  :class:`coder_eval.agents._delegate_s2s_token_file.S2sTokenFileRefresher`. An auth
  failure at init is non-retryable — the agent fails fast with an
  :class:`AgentConfigError` (distinguishing an *expired* saved login from an
  *absent* one) rather than burning the API-error retry budget.

Notes / limitations:

* ``token_usage`` is sourced from the framework's per-turn usage history,
  summed and forwarded by the host on each ``result`` message. The Delegate
  SDK does not expose pricing, so ``total_cost_usd`` is computed locally from
  the reported model and token counts via
  :func:`coder_eval.pricing.calculate_cost`; it stays ``None`` for
  models not in the pricing table.
* Per-message transcript: ``TurnRecord.messages`` carries one
  ``AssistantMessage`` per backend round-trip, reconstructed from the host's
  event stream by :class:`_TranscriptBuilder`, with per-generation token
  buckets zipped from the ``result`` message's ``turnUsages`` list (one entry
  per round-trip; their sum IS ``usage``). When the host predates
  ``turnUsages`` or the counts misalign, the messages carry content/timing but
  no tokens — the ``EventCollector``'s reconciliation entry then carries the
  turn total, preserving the transcript-sums-to-total invariant either way.
* ``allowed_tools``, ``disallowed_tools``, ``system_prompt``, ``system_prompt_file``,
  and ``setting_sources`` from :class:`DelegateSdkAgentConfig` have no Delegate-SDK
  equivalents; a warning is logged if any are set. ``permission_mode`` is equally
  unsupported but silently ignored — it always carries a truthy value, so warning
  on it would be noise on every run (see ``_UNSUPPORTED_FIELDS``).
* Plugins with a ``path`` field map to the SDK's ``bundledSkillsPath`` (assumed to be
  ``<plugin.path>/skills``). Multiple plugins: first wins; a warning is logged.
* Sandbox mock CLIs (``SandboxConfig.mock_path_dirs``, delivered to the agent ABC as
  ``env_path_prepend``) are forwarded as the ``shellPathPrepend`` init option, which
  the SDK injects into the environment of every shell command it runs inside the
  interop service. Requires a ``@uipath/delegate-stdio`` build whose bundled SDK
  understands ``shellPathPrepend``; older hosts ignore the option, in which case
  mock-graded docker-sandbox tasks fall back to the overlay image's cwd-aware ``uip``
  shim (host/tempdir and Windows runs have no shim, so there mock shadowing is simply
  lost). See :meth:`DelegateSdkAgent.start`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar, NoReturn
from urllib.parse import urlparse

from coder_eval.agent import Agent, AgentState
from coder_eval.agents._delegate_s2s_token_file import (
    GATEWAY_S2S_ENV_VARS,
    TOKEN_FILE_ENV_VARS,
    S2sTokenFileRefresher,
)
from coder_eval.agents._logging import PrefixedAdapter
from coder_eval.agents.registry import AgentRegistry
from coder_eval.errors import AgentConfigError, AgentCrashError, TurnTimeoutError, truncate_crash_message
from coder_eval.models import (
    AgentKind,
    ApiRoute,
    AssistantMessage,
    CommandTelemetry,
    ContentBlock,
    DelegateSdkAgentConfig,
    DirectRoute,
    SystemPromptSemantics,
    TokenUsage,
    TranscriptMessage,
    TurnRecord,
)
from coder_eval.pricing import calculate_cost
from coder_eval.streaming.callbacks import CompositeStreamCallback, StreamCallback
from coder_eval.streaming.collector import EventCollector
from coder_eval.streaming.events import (
    AgentEndEvent,
    AgentEndStatus,
    AgentStartEvent,
    TextChunkEvent,
    ToolEndEvent,
    ToolEndStatus,
    ToolStartEvent,
    TurnEndEvent,
    TurnEndStatus,
    TurnStartEvent,
)
from coder_eval.utils import process_plugins


logger = logging.getLogger(__name__)


# ---- Constants --------------------------------------------------------------

_STDIO_PACKAGE = "@uipath/delegate-stdio"
"""npm package (public npm registry) that ships the bundled host."""

_STDIO_BUNDLE_REL_PATH = Path("node_modules") / "@uipath" / "delegate-stdio" / "dist" / "delegate_stdio.mjs"
"""Path to the bundle inside an install dir's ``node_modules``."""


def _candidate_install_roots() -> list[Path]:
    """Install roots to probe for ``node_modules/@uipath/...`` when nothing is configured.

    Mirrors Node's own module resolution: start at the cwd and walk up through
    every ancestor directory. ``npm install <pkg>`` run in a directory with no
    ``package.json`` silently installs into the nearest *ancestor* that has one
    (frequently the user's home directory), so the bundle often lands above the
    cwd rather than in it. Home (``~``) is appended explicitly because it is not
    always an ancestor of the cwd (e.g. on Windows the cwd lives under
    ``C:\\source\\...`` while home is ``C:\\Users\\...``).
    """
    cwd = Path(os.getcwd()).resolve()
    roots: list[Path] = [cwd, *cwd.parents]
    home = Path.home().resolve()
    if home not in roots:
        roots.append(home)
    return roots


def _resolve_stdio_bundle() -> Path:
    """Locate the installed host bundle (``dist/delegate_stdio.mjs``).

    Resolution order:
      1. ``DELEGATE_STDIO_PATH`` — explicit absolute path to the bundle. CI and
         advanced setups set this directly (e.g. to the package's installed bin).
      2. ``DELEGATE_STDIO_NODE_MODULES`` — explicit install root; probed exactly
         (no walk-up — the operator told us where it is).
      3. Otherwise, walk up from the cwd through its ancestors (plus ``~``), the
         way Node resolves modules, so an ``npm install`` in any ancestor — or in
         home, where npm lands it when the cwd has no ``package.json`` — is found
         with zero configuration.

    The agent invokes Node directly on the bundle, getting ~1s cold start and
    avoiding any runtime CJS/ESM interop hazards from the dependency graph.
    """
    explicit = os.environ.get("DELEGATE_STDIO_PATH")
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not path.is_file():
            raise AgentConfigError(
                f"DELEGATE_STDIO_PATH={path} does not point to a file. Point it at the "
                + f"{_STDIO_PACKAGE} bundle (dist/delegate_stdio.mjs).",
            )
        return path

    base_override = os.environ.get("DELEGATE_STDIO_NODE_MODULES")
    if base_override:
        path = (Path(base_override).expanduser().resolve() / _STDIO_BUNDLE_REL_PATH).resolve()
        if not path.is_file():
            raise AgentConfigError(
                f"DELEGATE_STDIO_NODE_MODULES={base_override}: delegate-stdio bundle not found at {path}. "
                + f"Run `npm install {_STDIO_PACKAGE}` there, or set DELEGATE_STDIO_PATH to the bundle directly.",
            )
        return path

    searched: list[Path] = []
    for root in _candidate_install_roots():
        candidate = (root / _STDIO_BUNDLE_REL_PATH).resolve()
        searched.append(candidate)
        if candidate.is_file():
            return candidate

    searched_block = "\n  ".join(str(p) for p in searched)
    raise AgentConfigError(
        f"delegate-stdio bundle not found. Searched the cwd, its ancestors, and home:\n  {searched_block}\n"
        + f"Run `npm install {_STDIO_PACKAGE}` in {os.getcwd()} (installs into ./node_modules), "
        + "or set DELEGATE_STDIO_NODE_MODULES to the install root that holds node_modules/@uipath/..., "
        + "or set DELEGATE_STDIO_PATH to the bundle directly.",
    )


def _maybe_pin_npm_globalconfig(host_env: dict[str, str], plugin_tools_dir: str | None) -> Path | None:
    """Pin ``NPM_CONFIG_GLOBALCONFIG`` for the host so shells keep the global npmrc.

    The Delegate runtime's interop injects ``npm_config_prefix=~/.aria/npm/prefix``
    into every shell command it spawns (Aria's desktop guard against EACCES on
    read-only install dirs). Both npm and the uip CLI derive the global config as
    ``<prefix>/etc/npmrc``, so that injection silently relocates the lookup away
    from the real global npmrc — losing the ``@uipath:registry`` + auth token an
    image or operator baked there, and breaking on-demand ``uip tools install``
    ("No compatible version ... on npm or GitHub Packages"). Both npm and uip
    consult ``NPM_CONFIG_GLOBALCONFIG`` *before* the prefix-derived path, and the
    interop forwards inherited env untouched, so pinning it here restores the
    baked registry config for every shell in the host's process tree.

    Strictly additive: an operator-set value always wins, and when no global
    npmrc exists the env is left unchanged (there is nothing to pin — behavior
    identical to today). ``plugin_tools_dir`` is ``<node_modules>/@uipath`` of
    the npm installation that owns ``uip``; npm's global ``node_modules`` sits at
    ``<prefix>/lib/node_modules`` (POSIX) or ``<prefix>/node_modules`` (Windows),
    so both shapes are probed and the existence check disambiguates.

    Returns the pinned path, or None when nothing was pinned.
    """
    if "NPM_CONFIG_GLOBALCONFIG" in host_env or not plugin_tools_dir:
        return None
    node_modules = Path(plugin_tools_dir).resolve().parent
    for prefix in (node_modules.parent.parent, node_modules.parent):
        candidate = prefix / "etc" / "npmrc"
        if candidate.is_file():
            host_env["NPM_CONFIG_GLOBALCONFIG"] = str(candidate)
            return candidate
    return None


def _strip_gateway_creds(host_env: dict[str, str]) -> tuple[str, ...]:
    """Remove the eval's LLMGW_* gateway S2S credentials from the host env.

    Everything in the host env is inherited by the shells its interop spawns
    for the agent's own Bash / PowerShell tool calls, i.e. by the code under
    test. Withholding a live client secret from that surface is the
    least-privilege default; the docker driver already does the same by keeping
    LLMGW_* off run.py's env allowlist, so this gives host-run (tempdir) tasks
    the same treatment. When the pair is needed for token freshness, the
    adapter consumes it itself (see :class:`S2sTokenFileRefresher`) and hands
    the host a token FILE instead.

    Returns the names actually removed.
    """
    return tuple(name for name in GATEWAY_S2S_ENV_VARS if host_env.pop(name, None) is not None)


def _resolve_stall_timeout() -> float | None:
    """First-response stall ceiling in seconds, or ``None`` when disabled.

    Read from ``DELEGATE_STALL_TIMEOUT_S``. When set to a positive number, a
    turn that receives NO agent activity from the host within this many seconds
    of sending the prompt is treated as a transient backend stall: the wedged
    host is respawned and the prompt resent (see :meth:`DelegateSdkAgent.communicate`).
    Left unset (the default) the detection is off and ``communicate`` behaves
    exactly as before — so callers pointing the host at a dedicated, healthy
    backend (e.g. the Autopilot RPA eval's per-build ACI) are unaffected. The
    guard covers only the wait for the FIRST activity, so an in-flight tool
    execution (a long ``tool_call`` with no interim events) never trips it.
    """
    raw = os.environ.get("DELEGATE_STALL_TIMEOUT_S")
    if not raw:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        logger.warning("Ignoring non-numeric DELEGATE_STALL_TIMEOUT_S=%r; stall detection stays off", raw)
        return None
    return seconds if seconds > 0 else None


_VERBOSE_ENV_VAR = "DELEGATE_STDIO_VERBOSE"
"""Host tracing switch — read here AND by the host itself (it parses '1'/'true')."""

_VERBOSE_TRUTHY = frozenset({"1", "true"})
_VERBOSE_FALSY = frozenset({"0", "false"})


def _resolve_stdio_verbose() -> bool:
    """Whether the Delegate host should trace its stdio protocol to stderr.

    ``DELEGATE_STDIO_VERBOSE`` is the operator's switch and wins in BOTH
    directions: ``1``/``true`` forces tracing on even on a quiet run, and
    ``0``/``false`` forces it off even under ``--verbose``. Unset (the default)
    follows coder_eval's own verbosity, so ``--verbose`` traces and a normal run
    stays quiet.

    The vocabulary deliberately mirrors what the host's own ``DEBUG_LOGGING``
    gate parses, so the two sides can never disagree about what a value means;
    anything else warns and falls back to the ``--verbose`` default rather than
    guessing. Verbosity is read off this module's logger, a child of the
    ``coder_eval`` app logger that ``setup_logging`` configures, so ``--verbose``
    is exactly what flips it.
    """
    raw = os.environ.get(_VERBOSE_ENV_VAR, "").strip().lower()
    if raw in _VERBOSE_TRUTHY:
        return True
    if raw in _VERBOSE_FALSY:
        return False
    if raw:
        logger.warning(
            "Ignoring unrecognised %s=%r (expected one of %s); falling back to coder_eval's verbosity",
            _VERBOSE_ENV_VAR,
            raw,
            ", ".join(sorted(_VERBOSE_TRUTHY | _VERBOSE_FALSY)),
        )
    return logger.isEnabledFor(logging.DEBUG)


_STOP_TIMEOUT_SEC = 60.0
"""How long to wait for the host process to exit after `destroy` before killing it."""

_DEFAULT_STALL_RESENDS = 1
"""In-turn resend attempts when the host produces no output within the stall
window (see :func:`_resolve_stall_timeout`). One resend absorbs a transient
backend blip while staying inside the ``task_timeout`` budget; a persistent
stall then rides the normal turn/task timeout, surfacing a sustained backend
outage rather than masking it."""

_ACTIVITY_EVENT_TYPES = frozenset({"thinking", "message", "tool_call", "tool_result"})
"""Host event types that prove the backend round-trip is progressing. Receiving
any of these (or a ``result``) clears the first-response stall guard. Excludes
the informational ``session_start`` — the host can emit it *before* the backend
call, so it must not reset the guard."""

_STREAM_READER_LIMIT_BYTES = 64 * 1024 * 1024
"""Per-line buffer for the host's stdout/stderr StreamReaders (one ``limit=``
kwarg on ``create_subprocess_exec`` sizes both). asyncio's 64 KiB default is far
too small — a single ``result`` frame carries the agent's full reply, and tool
payloads reach multiple MB. 64 MiB keeps the protocol JSON intact while still
bounding runaway-output memory; the drain tasks catch ``LimitOverrunError``
beyond it (stdout treats the host as crashed, stderr drops the line)."""

_UNSUPPORTED_FIELDS = (
    "allowed_tools",
    "disallowed_tools",
    "system_prompt",
    "system_prompt_file",
    "setting_sources",
)
"""AgentConfig fields that have no Delegate SDK equivalent.

``permission_mode`` is intentionally absent: the Delegate SDK has no
permission-prompt concept, so silently ignoring it (any value) is fine. Its
default ``"acceptEdits"`` is truthy, so listing it here would emit a noisy
WARNING on every run."""


# ---- Auth-failure diagnostics ----------------------------------------------

_AUTH_ERROR_MARKERS = (
    "auth required",
    "authentication",
    "unauthorized",
    "invalid credentials",
    "invalid token",
    "invalid api key",
    "no auth",
    "expired",
    "401",
    "403",
)
"""Substrings (matched case-insensitively) that mark an init failure as an auth
problem. Auth failures never succeed on retry, so they short-circuit to a
non-retryable :class:`AgentConfigError` instead of the retryable AGENT_API_ERROR
the host's generic ``RuntimeError`` would otherwise be categorized as."""


def _is_auth_init_error(message: str) -> bool:
    """True when a host init-error message looks like an auth failure."""
    lowered = message.lower()
    return any(marker in lowered for marker in _AUTH_ERROR_MARKERS)


_SESSION_CONFLICT_MARKER = "already being generated"
"""Backend-409 fingerprint (matched case-insensitively): the SDK's own SSE
idle-watchdog / network-retry layer re-POSTed a message into a conversation
whose previous generation is still running, and the backend rejected it with
"A reply is already being generated for this conversation.". Resending into
the SAME conversation can only re-conflict, so every AGENT_CRASH retry burns
against the wedged conversation (build 12679882: 14 tasks died 3/3 attempts).

Dropping the adapter's ``_session_id`` alone is NOT enough (build 12685191:
4/7 Windows tasks still died): the failures hit on the task's FIRST turn, so
there is no session id to drop — and even with ``sessionId: null`` the live
host resumes the wedged conversation anyway, because the SDK's
``sendMessage(prompt, undefined)`` falls back to its in-memory
``currentSessionId``. The only clean recovery is a FRESH HOST:
:meth:`DelegateSdkAgent._crash` kills the wedged host on this marker and
:meth:`DelegateSdkAgent.communicate`'s entry guard respawns one for the
retry, whose new ``DelegateAgent`` starts with no current session."""


_WAF_BLOCK_PAGE_MARKERS = ("continue with uipath platform", "not available in your country")
"""Fingerprints (matched case-insensitively) of UiPath's generic Cloudflare
block page — ``<title>Continue with UiPath Platform</title>`` over "We are
sorry. UiPath platform is not available in your country." — served as an HTTP
403 when a managed WAF rule matches the REQUEST BODY. Despite the wording it is
NOT a geo/IP block: shell-like text in a tool result or prompt (e.g. a skill
doc's ``python -c "...open(...,'w')..."`` one-liner echoed back by ReadFile)
trips Cloudflare's command-injection rules in front of alpha.uipath.com, and
the request never reaches the backend (run adhoc-2026-07-24_16-51-27: 61 tasks,
47/47 uipath-maestro-case).

Two markers because the host truncates its error message at ~50 KB while the
page is ~68 KB: the visible text sits AFTER ~48 KB of base64 web-font CSS, so
the mid-turn tool-result PATCH shape — whose message is additionally prefixed
by the request URL — is cut before the country sentence ever appears. The
``<title>`` lands in the first ~300 bytes and survives both shapes; in that run
it matched 61/61 crash messages against the country text's 48/61."""


def _describe_waf_block(reason: str) -> str:
    """Concise, correctly-categorized replacement for a WAF-block crash reason.

    The raw reason embeds the whole block-page HTML, which is noise and — worse
    — reads as a geo/auth problem. The block is deterministic per payload (the
    retried turn re-sends the same tripping content), so the message stamps the
    framework categorizer's "content filter" signature, routing the failure to
    the non-retryable ``AGENT_INVALID_OUTPUT`` instead of burning AGENT_CRASH
    retries that fail identically.
    """
    prefix = reason.split("<", 1)[0].strip().rstrip(":").strip()
    return (
        "Backend request blocked by the Cloudflare WAF content filter in front of the UiPath backend "
        "(the generic 'not available in your country' 403 page — not a geo or auth problem): shell-like "
        "text in the prompt or a tool result matched a command-injection managed rule, and the same "
        "payload would be blocked again on retry. Fix by defanging shell one-liners in the skill docs "
        f"the task reads, or exempting the delegate_ route from the WAF managed rules. [{prefix}]"
    )


_SSE_CONNECT_TIMEOUT_MARKER = "sse connect timeout"
"""Fingerprint (matched case-insensitively) of the delegate host's SSE
connect/first-byte watchdog: a turn's POST got no HTTP response headers within
``SSE_CONNECT_TIMEOUT_MS`` (30s, agenticApi.ts), and the SDK's agentic loop has
already burned its 3 back-to-back retries — the error only surfaces after ~90s
of continuous backend front-door silence (build 12874194: both Windows failures
show the exact 3x30s signature, 90.0s from last tool result to error).

The raw message's "timeout" wording makes the framework categorizer route the
crash to AGENT_TIMEOUT — non-retryable, because for genuine task/turn *budget*
breaches a retry just re-burns the budget. This failure is neither: no headers
means the backend never started answering (streaming resets a separate 180s
idle watchdog once headers arrive, and the backend deliberately keeps the
pre-header phase minimal), so it marks a transient availability window that an
orchestrator retry with backoff usually lands outside of.
:meth:`DelegateSdkAgent._crash` rewrites the reason via
:func:`_describe_sse_connect_timeout` to stamp the "connection" signature
(→ retryable AGENT_API_ERROR, 5s/10s/20s backoff). The live host is kept — its
conversation context makes the resend a genuine continuation; if the resend
races a half-released backend turn claim, the resulting 409 lands in
:data:`_SESSION_CONFLICT_MARKER`'s fresh-host recovery."""


def _describe_sse_connect_timeout(reason: str) -> str:
    """Concise, correctly-categorized replacement for an SSE connect-watchdog crash reason.

    Stamps the framework categorizer's "connection" signature (→ retryable
    ``AGENT_API_ERROR``) and defangs every "timeout" occurrence so the earlier,
    non-retryable AGENT_TIMEOUT arm cannot match. The original watchdog wording
    (with its window length) is preserved defanged in brackets.
    """
    marker_at = reason.lower().find(_SSE_CONNECT_TIMEOUT_MARKER)
    original = reason[marker_at:] if marker_at >= 0 else reason
    defanged = re.sub("timeout", "time-out", original, flags=re.IGNORECASE)
    return (
        "Delegate backend connection failure: the turn's POST received no HTTP response headers within "
        "the host's SSE connect watchdog window, and the SDK's three back-to-back internal attempts all "
        "hit the same wall (~90s of continuous front-door silence) — a transient backend availability "
        "window, not a task-budget breach. The live host and its conversation are kept, so a delayed "
        f"resend continues where the turn left off. [{defanged}]"
    )


def _saved_auth_file() -> Path:
    """Path to the Delegate SDK's saved-login file (``~/.aria/sdk-auth.json``)."""
    return Path.home() / ".aria" / "sdk-auth.json"


def _format_duration(seconds: float) -> str:
    """Render a non-negative duration coarsely (days / hours / minutes)."""
    seconds = max(0.0, seconds)
    for unit_seconds, label in ((86_400.0, "day"), (3_600.0, "hour"), (60.0, "minute")):
        value = round(seconds / unit_seconds)
        if seconds >= unit_seconds:
            return f"~{value} {label}{'s' if value != 1 else ''}"
    return "less than a minute"


def _describe_saved_auth() -> str:
    """Describe the saved-login state for an auth-failure diagnostic.

    Distinguishes *absent* from *expired* — which the host's generic
    "Auth required" message cannot. Reads ``~/.aria/sdk-auth.json`` and, when its
    ``expiresAt`` (epoch seconds) is in the past, reports how long ago. The
    host has already attempted a token refresh (``loadAndRefreshAuth``) by the
    time we see the failure, so an expired file means the refresh failed too.
    Best-effort: never raises.
    """
    auth_file = _saved_auth_file()
    if not auth_file.is_file():
        return f"No saved login found at {auth_file}."
    try:
        data = json.loads(auth_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return f"Saved login at {auth_file} could not be read ({exc})."
    expires_at = data.get("expiresAt") if isinstance(data, dict) else None
    if not isinstance(expires_at, (int, float)) or isinstance(expires_at, bool):
        return f"Saved login found at {auth_file} (no expiry recorded)."
    age_seconds = time.time() - expires_at
    if age_seconds > 0:
        return f"Saved login at {auth_file} expired {_format_duration(age_seconds)} ago and could not be refreshed."
    return f"Saved login at {auth_file} is unexpired (expires in {_format_duration(-age_seconds)})."


# ---- Plugin / skills resolution --------------------------------------------


def _resolve_bundled_skills_path(plugins: list[dict[str, Any]] | None) -> str | None:
    """Translate coder_eval's plugin list to the SDK's ``bundledSkillsPath`` option.

    The Delegate SDK expects ``bundledSkillsPath`` to point at a directory whose
    direct children are skill folders (each containing a ``SKILL.md``). coder_eval
    plugins wrap that layout under ``<plugin>/skills/<skill>/SKILL.md``, so we append
    ``/skills`` to the plugin root.

    If multiple plugins are provided we use the first and log a warning listing the
    others — merging multiple plugin skill directories is out of scope for now.
    """
    expanded = process_plugins(plugins or [], log=logger)
    if not expanded:
        return None

    first = expanded[0]
    if "path" not in first:
        logger.warning("Delegate SDK plugin missing 'path' field — skipping: %r", first)
        return None

    if len(expanded) > 1:
        others = [p.get("path", repr(p)) for p in expanded[1:]]
        logger.warning(
            "Delegate SDK supports only one plugin; using %s and ignoring: %s",
            first["path"],
            ", ".join(others),
        )

    skills_path = Path(first["path"]) / "skills"
    return str(skills_path)


# ---- Transcript reconstruction ----------------------------------------------


def _usage_bucket_ints(raw: Any) -> tuple[int, int, int, int]:
    """Read the four token buckets off one host ``turnUsages`` entry.

    Returns ``(input, output, cache_creation, cache_read)``, zero for missing
    or invalid values — same tolerance as :meth:`DelegateSdkAgent._parse_usage`.
    """

    def _int(value: Any) -> int:
        return value if isinstance(value, int) and value >= 0 else 0

    if not isinstance(raw, dict):
        return 0, 0, 0, 0
    return (
        _int(raw.get("input_tokens")),
        _int(raw.get("output_tokens")),
        _int(raw.get("cache_creation_input_tokens")),
        _int(raw.get("cache_read_input_tokens")),
    )


class _GenerationSegment:
    """One LLM round-trip (generation) reconstructed from the host's event stream."""

    def __init__(self, started_at: datetime) -> None:
        self.started_at = started_at
        self.completed_at = started_at
        # (kind, payload) in emission order; kind is "thinking"/"text" (payload
        # is the accumulated text) or "tool_use" (payload is the tool_use_id).
        # Consecutive same-kind text payloads merge so streaming deltas form
        # one block instead of one block per token.
        self.blocks: list[list[str]] = []
        self.tool_use_ids: list[str] = []
        self.saw_tool_result = False

    def append_text(self, kind: str, text: str, now: datetime) -> None:
        if self.blocks and self.blocks[-1][0] == kind:
            self.blocks[-1][1] += text
        else:
            self.blocks.append([kind, text])
        self.completed_at = now

    def append_tool_use(self, tool_id: str, now: datetime) -> None:
        self.blocks.append(["tool_use", tool_id])
        self.tool_use_ids.append(tool_id)
        self.completed_at = now

    @property
    def has_text_or_tools(self) -> bool:
        return any(kind != "thinking" for kind, _ in self.blocks)


class _TranscriptBuilder:
    """Reconstruct per-generation ``AssistantMessage`` entries from host events.

    The Delegate host streams ``thinking`` / ``message`` / ``tool_call`` /
    ``tool_result`` events but carries no explicit generation-boundary marker,
    so boundaries are derived from stream order (the host forwards events on
    one ordered stdout pipe, preserving emission order):

    * a ``message`` event tagged ``isStepStart`` starts a new generation —
      unless the current one holds only thinking content, in which case the
      text belongs to the same round-trip (thinking streams before text);
    * any generation activity (thinking / message / tool_call) arriving after a
      ``tool_result`` belongs to the NEXT round-trip: the agentic loop is
      client-driven, so tool results are sent back in a fresh backend request.

    The reconstructed segment list lines up 1:1 with the host's
    ``turnUsages`` array (one entry per backend round-trip) in the normal case,
    which is what lets :meth:`build_messages` stamp real per-generation token
    buckets onto the transcript. When the counts disagree, stamping is skipped
    entirely — the ``EventCollector``'s reconciliation entry then carries the
    whole turn total, exactly as before — rather than risk misattributing
    buckets to the wrong generation.

    Timing follows :class:`ClaudeCodeAgent` semantics: a generation *starts* at
    the wall-clock arrival of the previous host event (which for round-trip
    N+1 is round-trip N's last ``tool_result`` — i.e. when the next backend
    request goes out) and *completes* at the arrival of its own last
    generation event. Tool execution time is therefore excluded.
    """

    def __init__(self) -> None:
        self._segments: list[_GenerationSegment] = []
        self._prev_event_at: datetime | None = None

    def _open_segment(self, now: datetime) -> _GenerationSegment:
        segment = _GenerationSegment(self._prev_event_at or now)
        self._segments.append(segment)
        return segment

    def _segment_for_generation(self, now: datetime) -> _GenerationSegment:
        """Current segment, or a fresh one when none is open / a round ended."""
        current = self._segments[-1] if self._segments else None
        if current is None or current.saw_tool_result:
            current = self._open_segment(now)
        return current

    def on_thinking(self, text: str) -> None:
        now = datetime.now()
        self._segment_for_generation(now).append_text("thinking", text, now)
        self._prev_event_at = now

    def on_message(self, text: str, *, is_step_start: bool) -> None:
        now = datetime.now()
        current = self._segments[-1] if self._segments else None
        # isStepStart only splits when the current segment already carries this
        # round's text or tools — a thinking-only segment means the round's
        # first text just arrived and joins it. Missing/False (streaming delta,
        # or an un-rebuilt host that never tags) appends to the current
        # segment so an old host degrades to merged text, not one segment
        # per streamed token.
        if current is None or current.saw_tool_result or (is_step_start and current.has_text_or_tools):
            current = self._open_segment(now)
        current.append_text("text", text, now)
        self._prev_event_at = now

    def on_tool_call(self, tool_id: str) -> None:
        now = datetime.now()
        self._segment_for_generation(now).append_tool_use(tool_id, now)
        self._prev_event_at = now

    def on_tool_result(self) -> None:
        now = datetime.now()
        if self._segments:
            self._segments[-1].saw_tool_result = True
        self._prev_event_at = now

    def final_text(self) -> str:
        """The assistant's final answer, assembled from the merged ``text`` blocks
        of the last text-bearing generation.

        Used as the ``agent_output`` fallback when the host's ``result`` message
        carries no authoritative ``response`` — e.g. a build that streams the
        final answer as deltas (each delta replaces, so the running scalar would
        keep only the last fragment), or a crash/timeout partial with no result
        message. Returns ``""`` when no text was produced.
        """
        for segment in reversed(self._segments):
            texts = [payload for kind, payload in segment.blocks if kind == "text"]
            if texts:
                return "".join(texts)
        return ""

    def build_messages(
        self,
        *,
        turn_id: str,
        model: str | None,
        turn_usages: Any,
        log: logging.Logger | logging.LoggerAdapter,  # type: ignore[type-arg]
    ) -> list[TranscriptMessage]:
        """Materialize the segments as ``AssistantMessage`` transcript entries.

        ``turn_usages`` is the host's per-round-trip usage list from the
        ``result`` message (``None`` for crash partials and older hosts).
        Token buckets are stamped only when it zips 1:1 onto the segments; the
        bucket sum then equals the turn total by construction (the host's
        ``usage`` is the sum of the same entries), so the collector's
        reconciliation entry vanishes instead of carrying the whole bill.

        Each message gets a synthetic per-generation ``message_id``
        (``{turn_id}-msg-{index}``, mirroring CodexAgent) so the evalboard
        groups exactly one row per generation instead of falling back to its
        wall-clock gap heuristic.
        """
        usages: list[Any] | None = turn_usages if isinstance(turn_usages, list) else None
        if usages is not None and len(usages) != len(self._segments):
            # Misattributing buckets is worse than not attributing: skip the
            # stamping wholesale and let the reconciliation entry carry the total.
            log.warning(
                "Host sent %d turnUsages entries but %d generations were reconstructed; skipping per-message tokens",
                len(usages),
                len(self._segments),
            )
            usages = None

        messages: list[TranscriptMessage] = []
        for index, segment in enumerate(self._segments):
            blocks: list[ContentBlock] = []
            for sequence, (kind, payload) in enumerate(segment.blocks):
                if kind == "thinking":
                    blocks.append(ContentBlock(block_type="thinking", sequence=sequence, thinking=payload))
                elif kind == "text":
                    blocks.append(ContentBlock(block_type="text", sequence=sequence, text=payload))
                else:
                    blocks.append(ContentBlock(block_type="tool_use", sequence=sequence, tool_use_id=payload))
            input_tokens, output_tokens, cache_creation, cache_read = _usage_bucket_ints(
                usages[index] if usages is not None else None
            )
            duration_ms = max(0.0, (segment.completed_at - segment.started_at).total_seconds() * 1000.0)
            messages.append(
                AssistantMessage(
                    started_at=segment.started_at,
                    completed_at=segment.completed_at,
                    generation_duration_ms=duration_ms,
                    content_blocks=blocks,
                    tool_use_ids=list(segment.tool_use_ids),
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_creation_tokens=cache_creation,
                    cache_read_tokens=cache_read,
                    model=model,
                    message_id=f"{turn_id}-msg-{index}",
                )
            )
        return messages


# ---- Per-turn mutable state ------------------------------------------------


@dataclass
class _TurnState:
    """Mutable accumulators + per-turn context for one ``communicate()`` call.

    Bundling these (previously ~12 ``nonlocal`` closures inside ``communicate``)
    into one object lets the per-message-type handling live in small methods that
    take the state explicitly, instead of one ~390-line god-method.
    """

    # Per-turn context (set once at construction).
    task_id: str
    turn_id: str
    turn_start: float
    user_input: str
    iteration: int
    emit: CompositeStreamCallback
    collector: EventCollector
    transcript: _TranscriptBuilder

    # Accumulators mutated while draining the host event stream.
    commands: dict[str, dict[str, Any]] = field(default_factory=dict)  # tool_id -> {telemetry, start_time}
    ended_tool_ids: set[str] = field(default_factory=set)
    sequence_number: int = 0
    # Incremented per ``isStepStart: true`` message event (one per LLM round-trip),
    # matching ClaudeCodeAgent's "one turn per AssistantMessage" semantics; the host
    # leaves streaming-delta events untagged so naive counting would inflate this.
    # Overwritten by the result message's authoritative ``assistantStepCount``.
    assistant_turn_count: int = 0
    model_used: str | None = None
    token_usage: TokenUsage | None = None
    max_turns_exhausted: bool = False
    error_message: str | None = None
    final_response: str = ""
    turn_usages: Any = None
    finalized: bool = False


# ---- The agent -------------------------------------------------------------


@AgentRegistry.register(AgentKind.DELEGATE_SDK, DelegateSdkAgentConfig)
class DelegateSdkAgent(Agent[DelegateSdkAgentConfig]):
    """Agent adapter that drives the UiPath Delegate SDK via a Node subprocess.

    See module docstring for prerequisites and limitations.
    """

    # The Delegate SDK has no system-prompt knob — ``system_prompt`` sits in
    # ``_UNSUPPORTED_FIELDS`` and is warned about at start() — so the honest
    # regime is ``"unknown"`` (also the base default). Declared explicitly so
    # the run marker is deliberate rather than an unset oversight.
    system_prompt_semantics: ClassVar[SystemPromptSemantics] = "unknown"

    def __init__(
        self,
        config: DelegateSdkAgentConfig,
        route: ApiRoute | None = None,
        *,
        instance_name: str = "delegate",
    ) -> None:
        """Initialise the agent.

        Args:
            config: Resolved task agent configuration.
            route: Accepted for API symmetry with :class:`ClaudeCodeAgent`; ignored.
                The Delegate SDK has its own UiPath auth path (not Anthropic-style routing).
            instance_name: Short label used to prefix this instance's log records.
        """
        self.config = config
        self.route = route or DirectRoute()  # stored for diagnostics only
        self.working_directory: Path | None = None

        # Turn-lifecycle state (_state / _iteration / _iteration_was_incremented /
        # pending_turn) lives on the Agent base class — managed via _begin_turn()
        # / _end_turn_ok() / discard_pending_turn() / _mark_stopped().
        self._session_id: str | None = None

        self._process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stdout_task: asyncio.Task[None] | None = None
        self._stdout_queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        # Bounded ring buffer: the verbose writeLine echo can stream many large
        # lines to stderr; only the last entries feed the crash-message tail.
        self._stderr_lines: deque[str] = deque(maxlen=200)
        self._init_options: dict[str, Any] | None = None
        self._stdio_bundle: Path | None = None
        # Cached so the host can be re-established mid-turn without re-running
        # start() (the orchestrator retries communicate() only). Populated by
        # start(); consumed by _spawn_and_init()/_respawn_host().
        self._host_env: dict[str, str] | None = None
        self._stall_timeout: float | None = None
        # Set by _crash() on a backend session conflict (the wedged host was
        # killed); tells communicate()'s entry guard to respawn a fresh host
        # for the AGENT_CRASH retry instead of failing fast.
        self._respawn_before_retry = False
        # Keeps the host's token file fresh past the ~1h S2S TTL (None when the
        # run cannot safely self-refresh — see S2sTokenFileRefresher.maybe_create).
        self._token_refresher: S2sTokenFileRefresher | None = None
        self._log = PrefixedAdapter(logger, {"prefix": instance_name})
        # Level at which _drain_stderr forwards the host's stderr lines. start()
        # raises it to INFO when DELEGATE_STDIO_VERBOSE forces tracing on a quiet
        # run, where DEBUG records would be dropped by the app's INFO handlers.
        self._host_stderr_log_level = logging.DEBUG

    # -- Agent ABC -----------------------------------------------------------

    async def start(
        self,
        working_directory: str,
        *,
        env_path_prepend: list[str] | None = None,
        plugin_tools_dir: str | None = None,
    ) -> None:
        """Spawn the host subprocess and initialise the Delegate agent.

        ``env_path_prepend`` and ``plugin_tools_dir`` are part of the
        :class:`Agent` ABC for agents that shell out (sandbox PATH injection
        and pinning the UiPath CLI's plugin discovery via ``PLUGIN_TOOLS_DIR``,
        respectively). ``env_path_prepend`` (the sandbox's mock_path_dirs —
        e.g. a ``mocks/uip`` wrapper that must shadow the real CLI) is
        forwarded to the host as the ``shellPathPrepend`` init option rather
        than applied to this process: shell tools execute inside the interop
        service, whose own PATH was fixed at spawn (and which is deliberately
        reused across runs), so a prepend here could never reach them. The SDK
        delivers it through the per-command environment interop applies to each
        shell child — see the Autopilot-side ``runtime/shellPathEnv.ts``. Hosts
        whose bundled SDK predates the option ignore it; docker-sandbox tasks
        then fall back to the overlay image's cwd-aware ``uip`` shim
        (uip-mock-shim.sh), which dispatches to the nearest ``mocks/uip`` above
        the command's cwd — host/tempdir and Windows runs don't install that
        shim, so on those paths mock shadowing is lost entirely.
        ``plugin_tools_dir`` is used only as the anchor for the
        ``NPM_CONFIG_GLOBALCONFIG`` pin (see
        :func:`_maybe_pin_npm_globalconfig`).
        """
        # The orchestrator retries start() on a retryable init failure without an
        # intervening stop(); reclaim any subprocess/drain tasks from a prior
        # attempt first so a retry can't orphan the previous Node host.
        await self._teardown_host()
        self.working_directory = Path(working_directory)
        self._state = AgentState.WORKING

        self._warn_unsupported_fields()

        bundle_path = _resolve_stdio_bundle()
        self._stdio_bundle = bundle_path

        # Host tracing + our own log visibility are one decision: the host writes
        # its trace to stderr and _drain_stderr forwards it line by line. Under
        # --verbose this module's logger (a child of the ``coder_eval`` app
        # logger) is already at DEBUG, so the forwarded lines reach the run log
        # as DEBUG records. When the operator forces DELEGATE_STDIO_VERBOSE on a
        # QUIET run, DEBUG records would be dropped by the app's INFO-level
        # handlers — so the trace is forwarded at INFO instead, which is the only
        # way the switch can deliver what it promises.
        stdio_verbose = _resolve_stdio_verbose()
        self._host_stderr_log_level = (
            logging.INFO if stdio_verbose and not logger.isEnabledFor(logging.DEBUG) else logging.DEBUG
        )

        self._init_options = self._build_init_options(env_path_prepend=list(env_path_prepend or []))

        host_env = {**os.environ}
        # Normalise to the literal the host's own gate parses, or remove the var
        # outright. The host reads it from this inherited env, so forwarding an
        # operator's raw value verbatim would let the two sides disagree, and an
        # explicit opt-out has to actually erase an inherited truthy value.
        if stdio_verbose:
            host_env[_VERBOSE_ENV_VAR] = "1"
            self._log.debug("Delegate host stdio tracing ON (%s): one log line per host frame", _VERBOSE_ENV_VAR)
        else:
            host_env.pop(_VERBOSE_ENV_VAR, None)
        # Keep uip/npm registry auth reachable from the shells the Delegate
        # runtime spawns (its interop overrides npm_config_prefix per command,
        # which would otherwise orphan the global npmrc — see the helper).
        if pinned := _maybe_pin_npm_globalconfig(host_env, plugin_tools_dir):
            self._log.debug("Pinned NPM_CONFIG_GLOBALCONFIG=%s for the Delegate host", pinned)
        # Keep the eval's gateway S2S secret out of the agent's own shell tools.
        if stripped := _strip_gateway_creds(host_env):
            self._log.debug("Stripped %s from the Delegate host env", stripped)
        # Token freshness: the host owns applying it via DELEGATE_AUTH_TOKEN_FILE
        # (re-read at init, at every turn, and before each TTL boundary). When an
        # external refresher already maintains that file, nothing to do here.
        # Otherwise — runs whose AUTH_TOKEN was minted from the very LLMGW pair
        # stripped above (the rpa-eval pipeline) — the host's own S2S refresh is
        # disabled BY the strip, so the adapter takes over: it re-mints
        # adapter-side and publishes through a token file, keeping the secret out
        # of the host env while restoring freshness.
        # Gated against the adapter's own env, not host_env: the strip above just
        # removed the LLMGW_* pair the gate needs to read.
        if refresher := S2sTokenFileRefresher.maybe_create(os.environ, self._log):
            try:
                token_file = await refresher.start()
            except Exception as error:
                # Freshness is an enhancement over the inherited AUTH_TOKEN, so a
                # refresher that cannot even start (a full or read-only tempdir,
                # say) must not fail `Agent start` — that would turn runs which
                # finish inside the ~1h TTL into ERRORs the orchestrator retries
                # against the same broken condition.
                self._log.warning(
                    "S2S token-file refresher unavailable (%s) — continuing with the inherited AUTH_TOKEN", error
                )
                await refresher.stop()
            else:
                self._token_refresher = refresher
                for name in TOKEN_FILE_ENV_VARS:
                    host_env[name] = token_file
                self._log.info("S2S token-file refresher active — host token file: %s", token_file)
        # Cached so a mid-turn respawn can rebuild an identical host without
        # re-running start() (the env is stable across a task).
        self._host_env = host_env
        self._stall_timeout = _resolve_stall_timeout()

        await self._spawn_and_init()

    async def _spawn_and_init(self) -> None:
        """Spawn the Node host subprocess, wire the drain tasks, and run the init handshake.

        The spawn tail shared by :meth:`start` (first launch) and
        :meth:`_respawn_host` (mid-turn stall recovery). Reuses the cached
        ``_stdio_bundle`` / ``_host_env`` / ``_init_options`` from ``start``.
        A fresh ``_stdout_queue`` is installed first so a stale EOF sentinel
        left by a previously-crashed host can't be read as this host's output.

        @raises AgentConfigError: init failed for an auth reason (non-retryable).
        @raises RuntimeError: init failed for any other reason.
        """
        assert self._stdio_bundle is not None, "_spawn_and_init requires a resolved bundle (call start first)"
        assert self._host_env is not None, "_spawn_and_init requires a built host env (call start first)"
        bundle_path = self._stdio_bundle
        # Drop any EOF sentinel a prior (crashed) host's drain task queued.
        self._stdout_queue = asyncio.Queue()

        self._process = await asyncio.create_subprocess_exec(
            "node",
            str(bundle_path),
            # cwd is host-adjacent (not the eval sandbox): Node resolves
            # @uipath/delegate-sdk relative to the bundle file, not cwd. The
            # host itself chdir()s into options["workingDirectory"] during
            # init, so tools still operate from the sandbox.
            cwd=str(bundle_path.parent),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._host_env,
            limit=_STREAM_READER_LIMIT_BYTES,
        )
        assert self._process.stderr is not None
        assert self._process.stdout is not None
        self._stderr_task = asyncio.create_task(self._drain_stderr(self._process.stderr))
        self._stdout_task = asyncio.create_task(self._drain_stdout(self._process.stdout))

        await self._send_command({"cmd": "init", "options": self._init_options})
        ack = await self._read_until(("init_ok", "error"))
        if ack.get("type") == "error":
            message = str(ack.get("message", "unknown error"))
            if _is_auth_init_error(message):
                # Auth failures never succeed on retry. Raise the non-retryable
                # AgentConfigError (the categorizer routes it to
                # AGENT_CONFIG_ERROR) so the run fails fast instead of burning
                # ~40s on AGENT_API_ERROR's exponential backoff — mirroring the
                # host-not-found path — and enrich it with the saved-login's
                # expiry so "expired" is distinguishable from "absent".
                raise AgentConfigError(self._format_auth_init_error(message))
            raise RuntimeError(f"Delegate SDK init failed: {message}")

    async def _respawn_host(self) -> None:
        """SIGKILL the wedged host and start a fresh one for in-turn stall recovery.

        A host stuck on a backend round-trip cannot be unblocked, so it is
        force-killed (a graceful ``destroy`` would hang too) and re-initialised
        via :meth:`_spawn_and_init`. The delegate ``sessionId`` is left intact so
        the resent prompt resumes the conversation on backends that persist
        sessions. Propagates :meth:`_spawn_and_init`'s init failures.
        """
        await self._force_kill_host()
        await self._cancel_drain_tasks()
        self._process = None
        await self._spawn_and_init()

    async def communicate(
        self,
        user_input: str,
        *,
        stream_callback: StreamCallback | None = None,
        timeout: float | None = None,
        max_turns: int | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> TurnRecord:
        """Send one turn to the Delegate agent and return a :class:`TurnRecord`.

        Args:
            user_input: The message/prompt to send.
            stream_callback: Optional callback for real-time event streaming.
            timeout: Hard wall-clock deadline in seconds. When exceeded, the
                host subprocess is force-killed and :class:`TurnTimeoutError`
                is raised; a ``crashed=True`` partial :class:`TurnRecord` is
                stashed on ``self.pending_turn`` for the orchestrator to drain.
            max_turns: Per-call cap on inner-loop turns, forwarded to the
                host as ``maxSteps``. ``None`` defers to the SDK default.
            should_stop: Accepted for ``Agent.communicate`` override
                compatibility and ignored — the Delegate host drives its own
                inner loop, so there is no between-message poll point here
                (``supports_cooperative_stop`` stays False).
        """
        if not self.working_directory:
            raise RuntimeError("Agent not started. Call start() first.")
        if self._process is None:
            if self._respawn_before_retry and self._host_env is not None and self._stdio_bundle is not None:
                # The previous attempt crashed on a backend session conflict
                # and _crash() killed the wedged host (its in-memory
                # currentSessionId would resume the wedged conversation on any
                # resend). Respawn a fresh host so this AGENT_CRASH retry runs
                # in a genuinely fresh conversation — the only attempt shape
                # that can succeed (build 12685191). Init failures propagate
                # typed (AgentConfigError for auth, RuntimeError otherwise).
                self._respawn_before_retry = False
                self._log.warning(
                    "Respawning a fresh Delegate host for this retry — the previous host was killed "
                    + "after a backend session conflict (its conversation is wedged behind a "
                    + "still-running generation)"
                )
                await self._respawn_host()
            else:
                # The host died in a previous turn (the crash/timeout branches
                # below drop the handle). AGENT_CRASH retries re-enter
                # communicate() without re-running start() — the only place the
                # host is spawned — so fail fast with the typed crash error (the
                # orchestrator's drain/discard hook still fires) instead of
                # writing into a broken pipe (mis-categorized as a retryable
                # AGENT_API_ERROR) or blocking forever on the already-consumed
                # EOF sentinel.
                raise AgentCrashError(
                    "Delegate SDK host subprocess is not running — it crashed or timed out "
                    + "in a previous turn, and retries do not respawn it."
                )

        assert self.config.type is not None, "DelegateSdkAgent requires AgentConfig.type to be set before communicate()"

        # Reset the pending slot + bump the iteration counter (shared lifecycle).
        self._begin_turn()
        turn_start = time.monotonic()
        deadline = turn_start + timeout if timeout is not None else None

        # Event emission: the agent is the SOLE emitter; events fan out to an
        # internal EventCollector (which assembles the returned TurnRecord — the
        # single, agent-agnostic capture path) and the caller's stream_callback.
        task_id = self.config.type
        collector = EventCollector()
        # Rebuilds the per-generation AssistantMessage transcript from the
        # host's event stream; the messages ride the terminal AgentEndEvent's
        # finalization payload (the collector reads them back verbatim).
        transcript = _TranscriptBuilder()
        emit = CompositeStreamCallback([c for c in (collector, stream_callback) if c is not None])
        # The host runs an inner step loop, but the cross-agent TurnRecord
        # treats one communicate() as one turn (assistant_turn_count carries the
        # step count authoritatively), so a single turn_id covers the call.
        turn_id = f"delegate-{self._iteration}"

        # All per-turn accumulators + context live on one mutable state object so
        # the per-message-type handling can live in small methods (below) instead
        # of one ~390-line god-method of nonlocal closures. model_used starts as
        # the task-configured model so reports always have something to display;
        # the host overwrites it from the ``result`` message.
        st = _TurnState(
            task_id=task_id,
            turn_id=turn_id,
            turn_start=turn_start,
            user_input=user_input,
            iteration=self._iteration,
            emit=emit,
            collector=collector,
            transcript=transcript,
            model_used=self.config.model,
        )

        # Everything from the opening events onward runs inside the try so that
        # EVERY exit path — including a pre-loop send failure — flows through
        # _finalize (terminal AgentEndEvent + pending_turn stash) per the Agent
        # contract, mirroring ClaudeCodeAgent's finally-finalize.
        try:
            emit.on_event(
                AgentStartEvent(task_id=task_id, prompt=user_input, iteration=self._iteration, model=st.model_used)
            )
            emit.on_event(TurnStartEvent(task_id=task_id, turn_id=turn_id, model=st.model_used))

            send_payload: dict[str, Any] = {
                "cmd": "send",
                "prompt": user_input,
                "sessionId": self._session_id,
            }
            if max_turns is not None:
                send_payload["maxSteps"] = max_turns
            await self._send_command(send_payload)

            # First-response stall recovery (opt-in via DELEGATE_STALL_TIMEOUT_S).
            # A wedged backend round-trip emits no events; without this it rides
            # the non-retryable task/turn timeout to a score-0 loss. Bounded
            # in-turn respawn+resend turns a transient stall into a recovered
            # turn; a persistent one still falls through to the timeout.
            resends_left = _DEFAULT_STALL_RESENDS if self._stall_timeout is not None else 0
            first_activity_seen = False
            while True:
                read_deadline, stall_capped = self._first_activity_read_deadline(
                    deadline, first_activity_seen=first_activity_seen, resends_left=resends_left
                )
                try:
                    msg = await self._read_next_message(read_deadline)
                except TimeoutError:
                    if not stall_capped:
                        raise  # genuine turn deadline — handled by the outer timeout branch
                    resends_left -= 1
                    self._log.warning(
                        "Delegate host produced no output within %.0fs of the prompt; respawning the "
                        + "host and resending (transient backend stall; %d resend(s) left)",
                        self._stall_timeout,
                        resends_left,
                    )
                    await self._respawn_host()
                    await self._send_command(send_payload)
                    continue

                if msg is None:
                    # Host subprocess exited mid-turn (EOF sentinel from drain task).
                    # Drop the dead handle so a retried communicate() fails fast in
                    # the entry guard (retries never respawn the host).
                    self._crash(st, self._build_crash_message(), drop_process=True)
                mtype = msg.get("type")

                if mtype == "event":
                    event = msg.get("event") or {}
                    if event.get("type") in _ACTIVITY_EVENT_TYPES:
                        first_activity_seen = True
                    self._handle_event(st, event)
                elif mtype == "result":
                    first_activity_seen = True
                    self._handle_result(st, msg)
                    break
                elif mtype == "error":
                    self._crash(
                        st,
                        f"Delegate SDK reported error during turn: {msg.get('message', 'unknown error')}",
                        from_host_error=True,
                    )
                else:
                    self._log.debug("Ignoring unknown host message: %r", msg)

            # (loop exits via ``break`` on the result message)
            if st.error_message is not None:
                # The host process is still alive after reporting a turn error, so
                # the handle is kept — an AGENT_CRASH retry can legitimately reuse
                # the live host (transient backend errors do recover on resend).
                # Exception: a session-conflict error makes _crash kill the host,
                # because the live host would resume the wedged conversation.
                self._crash(st, f"Communication with Delegate SDK failed: {st.error_message}", from_host_error=True)

            # A clean turn returns the agent to WORKING — including after a prior
            # attempt set ERROR and was retried (AGENT_CRASH / AGENT_API_ERROR are
            # retryable). The orchestrator's discard_pending_turn() rolls back the
            # iteration but does not touch _state, so without this reset a retried
            # success would still report ERROR via get_state(). Mirrors
            # ClaudeCodeAgent (_update_state_from_messages) and CodexAgent's
            # success-path reset.
            self._state = AgentState.WORKING

            if st.max_turns_exhausted:
                self._log.warning(
                    "Delegate SDK halted after %d step(s) — max_turns cap reached before the agent completed",
                    st.assistant_turn_count,
                )

            # _finalize_turn emits the terminal TurnEnd + AgentEnd boundary (the
            # finalization payload the collector reads back) and force-closes any
            # tool calls that never received a result.
            self._finalize_turn(st, AgentEndStatus.COMPLETED, crashed=False, crash_reason=None)

        except (AgentCrashError, TurnTimeoutError):
            raise  # already finalized at the raise site (via _crash / the timeout branch)
        except TimeoutError:
            # Wall-clock deadline hit. Kill the host, stash a partial crashed=True
            # TurnRecord on pending_turn so the orchestrator can drain it, then raise
            # TurnTimeoutError per the Agent contract.
            assert timeout is not None  # only enters this branch when deadline is set
            await self._force_kill_host()
            self._state = AgentState.ERROR
            self._finalize_turn(st, AgentEndStatus.TIMEOUT, crashed=True, crash_reason=f"turn timeout after {timeout}s")
            # Drop the killed handle so a retried communicate() fails fast in the
            # entry guard (retries never respawn the host).
            self._process = None
            raise TurnTimeoutError(timeout, iteration=self._iteration) from None
        except Exception as exc:
            # Anything else mid-turn — e.g. the stdin pipe breaking during the
            # pre-loop send. Without this wrap a bare BrokenPipeError would be
            # mis-categorized as a retryable AGENT_API_ERROR whose retry never
            # drains the partial or rolls the iteration back.
            self._crash(st, f"Communication with Delegate SDK failed: {exc}", cause=exc)
        finally:
            # Terminal-event guarantee (Agent contract: AgentEndEvent on EVERY exit
            # path). The handlers above finalize every Exception route, so this fires
            # only on non-Exception exits (e.g. an external CancelledError) — still
            # close the event tree before propagating.
            if not st.finalized:
                self._finalize_turn(
                    st, AgentEndStatus.CRASHED, crashed=True, crash_reason="turn aborted before completion"
                )

        # Turn completed cleanly — the iteration bump stands.
        self._end_turn_ok()

        # The returned TurnRecord is the EventCollector's reduction of the emitted
        # events — single, agent-agnostic capture path.
        return collector.build_turn_record()

    # -- communicate() helpers (per-message-type handling) -------------------

    async def _read_next_message(self, deadline: float | None) -> dict[str, Any] | None:
        """Read one host message, honouring the wall-clock ``deadline``.

        Raises :class:`TimeoutError` if the deadline has already passed (or
        ``wait_for`` trips it mid-read); returns ``None`` on the EOF sentinel.
        """
        if deadline is None:
            return await self._read_line()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        return await asyncio.wait_for(self._read_line(), timeout=remaining)

    def _first_activity_read_deadline(
        self, turn_deadline: float | None, *, first_activity_seen: bool, resends_left: int
    ) -> tuple[float | None, bool]:
        """Deadline for the next host read, and whether it is the stall cap.

        While still awaiting the first agent activity of a turn (and a resend is
        still budgeted), the wait is capped at ``_stall_timeout`` so a wedged
        backend round-trip is caught quickly. Returns ``(deadline, stall_capped)``
        where ``stall_capped`` is True only when the returned deadline is the
        stall cap and strictly tighter than the real turn deadline — the signal
        that a :class:`TimeoutError` should trigger respawn+resend rather than a
        turn timeout. When stall detection is off, activity has been seen, or the
        resend budget is spent, the real ``turn_deadline`` is returned unchanged.
        """
        stall = self._stall_timeout
        if stall is None or first_activity_seen or resends_left <= 0:
            return turn_deadline, False
        stall_deadline = time.monotonic() + stall
        if turn_deadline is not None and turn_deadline <= stall_deadline:
            return turn_deadline, False
        return stall_deadline, True

    def _handle_event(self, st: _TurnState, event: dict[str, Any]) -> None:
        """Dispatch one host ``event`` message to the right per-type handler.

        ``session_start`` / ``done`` are informational and ignored.
        """
        etype = event.get("type")
        if etype in ("thinking", "message"):
            self._handle_text_event(st, event)
        elif etype == "tool_call":
            self._handle_tool_call(st, event)
        elif etype == "tool_result":
            self._handle_tool_result(st, event)
        elif etype == "error":
            st.error_message = str(event.get("error") or "unknown error")

    def _handle_text_event(self, st: _TurnState, event: dict[str, Any]) -> None:
        """Thinking / assistant-message text: feed the transcript + stream it out."""
        text = str(event.get("content") or "")
        if event.get("type") == "message":
            # The host tags new LLM round-trips with isStepStart=True; streaming-delta
            # events for the same round-trip arrive with isStepStart=False. Older host
            # builds don't emit the flag at all, so a missing field falls back to
            # "count it as a turn" to preserve behaviour against an un-rebuilt host.
            is_step_start = event.get("isStepStart")
            if is_step_start is None or bool(is_step_start):
                st.assistant_turn_count += 1
            if text:
                st.transcript.on_message(text, is_step_start=bool(is_step_start))
        elif text:
            st.transcript.on_thinking(text)
        if text:
            st.emit.on_event(TextChunkEvent(task_id=st.task_id, turn_id=st.turn_id, text=text))

    def _handle_tool_call(self, st: _TurnState, event: dict[str, Any]) -> None:
        """Open a tool call: record telemetry + emit ToolStart."""
        tool_name = str(event.get("toolName") or "")
        tool_args = event.get("toolArgs") or {}
        tool_id = str(event.get("toolId") or uuid.uuid4())
        telemetry = CommandTelemetry(
            tool_name=tool_name,
            tool_id=tool_id,
            timestamp=datetime.now(),
            parameters=tool_args if isinstance(tool_args, dict) else {"raw": tool_args},
            sequence_number=st.sequence_number,
            result_status=None,
            duration_ms=None,
        )
        st.commands[tool_id] = {"telemetry": telemetry, "start_time": time.monotonic()}
        st.transcript.on_tool_call(tool_id)
        st.emit.on_event(ToolStartEvent(task_id=st.task_id, turn_id=st.turn_id, tool=telemetry))
        st.sequence_number += 1

    def _handle_tool_result(self, st: _TurnState, event: dict[str, Any]) -> None:
        """Close a tool call: resolve the pending telemetry + emit ToolEnd."""
        # Round-trip boundary marker: any generation activity after a tool result
        # belongs to the next round-trip.
        st.transcript.on_tool_result()
        tool_name = str(event.get("toolName") or "")
        tool_id = str(event.get("toolId") or "")
        result_raw = event.get("toolResult")
        is_error = (event.get("toolStatus") or "") == "failed"
        matched_id = tool_id if tool_id in st.commands else self._find_pending_by_name(st.commands, tool_name)
        if not matched_id:
            self._log.debug("tool_result for unknown tool %s (id=%s); ignoring", tool_name, tool_id)
            return
        telem = st.commands[matched_id]["telemetry"]
        telem.result_status = "error" if is_error else "success"
        telem.duration_ms = (time.monotonic() - st.commands[matched_id]["start_time"]) * 1000
        result_text = str(result_raw) if result_raw is not None else ""
        # Stored untruncated to match ClaudeCodeAgent and the documented invariant
        # (CLAUDE.md): result_summary is kept whole so sub-agent returns are
        # preserved. The 64 MiB StreamReader limit already bounds a pathological
        # payload upstream.
        telem.result_summary = result_text or None
        if is_error:
            telem.error_message = result_text
        st.emit.on_event(
            ToolEndEvent(
                task_id=st.task_id,
                turn_id=st.turn_id,
                tool=telem,
                status=ToolEndStatus.ERROR if is_error else ToolEndStatus.OK,
            )
        )
        st.ended_tool_ids.add(matched_id)

    def _handle_result(self, st: _TurnState, msg: dict[str, Any]) -> None:
        """Terminal ``result`` message: fold in the authoritative turn totals."""
        response = msg.get("response")
        if isinstance(response, str) and response:
            st.final_response = response
        session_id = msg.get("sessionId")
        if isinstance(session_id, str):
            self._session_id = session_id
        # Authoritative per-turn step count from the host — supersedes the running
        # counter, which is only kept current so crash/timeout partials have a value.
        step_count = msg.get("assistantStepCount")
        if isinstance(step_count, int) and step_count >= 0:
            st.assistant_turn_count = step_count
        model_value = msg.get("model")
        if isinstance(model_value, str) and model_value:
            st.model_used = model_value
        st.token_usage = self._parse_usage(msg.get("usage"), st.model_used) or st.token_usage
        # Per-round-trip usage entries (sum == `usage`); zipped onto the transcript
        # in _finalize_turn. Absent on older hosts, which fall back to token-less
        # messages.
        st.turn_usages = msg.get("turnUsages")
        st.max_turns_exhausted = bool(msg.get("maxStepsReached"))

    def _crash(
        self,
        st: _TurnState,
        reason: str,
        *,
        drop_process: bool = False,
        cause: BaseException | None = None,
        from_host_error: bool = False,
    ) -> NoReturn:
        """Mark ERROR, finalize a crashed=True partial, optionally drop the host
        handle, and raise :class:`AgentCrashError`.

        Consolidates the four mid-turn crash sites (EOF sentinel, host ``error``
        message, post-loop ``error_message``, generic exception) so they can't
        drift — e.g. one site forgetting ``truncate_crash_message`` or the ERROR
        state set. ``drop_process`` nulls the (dead) handle so a retried
        communicate() fails fast in the entry guard; ``cause`` chains the
        original exception for the generic-exception site.

        A backend session-conflict crash (see :data:`_SESSION_CONFLICT_MARKER`)
        additionally kills the host and drops ``_session_id``: the conversation
        is wedged behind its own still-running generation, and the live host
        would resume it even on a ``sessionId: null`` resend (the SDK falls
        back to its in-memory ``currentSessionId``), so a fresh host — which
        the entry guard respawns via ``_respawn_before_retry`` — is the only
        attempt shape that can succeed.

        A Cloudflare WAF block (see :data:`_WAF_BLOCK_PAGE_MARKERS`) instead
        gets its reason rewritten via :func:`_describe_waf_block`: the block is
        deterministic per payload, so no retry shape can succeed — the rewrite
        stamps the "content filter" signature that routes the failure to the
        non-retryable ``AGENT_INVALID_OUTPUT`` category and explains the real
        cause instead of the misleading country/auth wording.

        An SSE connect-watchdog failure (see :data:`_SSE_CONNECT_TIMEOUT_MARKER`)
        gets the inverse treatment via :func:`_describe_sse_connect_timeout`:
        the raw "timeout" wording would route it to the non-retryable
        ``AGENT_TIMEOUT``, but it marks a transient backend availability window,
        so the rewrite stamps the "connection" signature (→ retryable
        ``AGENT_API_ERROR``) while keeping the live host for the resend. That
        rewrite requires ``from_host_error``: unlike the HTML-page WAF markers,
        the SSE fingerprint is a log-line-shaped string, so it also appears in
        the 20-line host stderr tail that :meth:`_build_crash_message` embeds —
        and a dead-host crash whose stderr merely *mentions* the watchdog (even
        one the SDK recovered from) must keep its own reason. Rewriting it there
        would drop the exit code, assert a live host that the same call nulls,
        and flip a terminal crash into retries that the entry guard can only
        fail.
        """
        lowered = reason.lower()
        if any(marker in lowered for marker in _WAF_BLOCK_PAGE_MARKERS):
            reason = _describe_waf_block(reason)
            self._log.warning(reason)
        elif from_host_error and _SSE_CONNECT_TIMEOUT_MARKER in lowered and _SESSION_CONFLICT_MARKER not in lowered:
            # Session-conflict takes precedence when a reason carries both
            # fingerprints: only the fresh-host shape below can recover it.
            reason = _describe_sse_connect_timeout(reason)
            self._log.warning(reason)
        elif _SESSION_CONFLICT_MARKER in lowered:
            self._log.warning(
                "Backend reports the conversation (session %s) is still generating a reply; killing the "
                + "wedged host so a retry starts a fresh one — a live host would resume the same wedged "
                + "conversation and re-conflict",
                self._session_id or "<first turn — no id yet>",
            )
            self._session_id = None
            self.kill_sync()
            drop_process = True
            self._respawn_before_retry = True
        self._state = AgentState.ERROR
        self._finalize_turn(st, AgentEndStatus.CRASHED, crashed=True, crash_reason=truncate_crash_message(reason))
        if drop_process:
            self._process = None
        raise AgentCrashError(reason) from cause

    def _finalize_turn(
        self, st: _TurnState, status: AgentEndStatus, *, crashed: bool, crash_reason: str | None
    ) -> None:
        """Emit the terminal TurnEnd + AgentEnd boundary exactly once.

        On crash/timeout it also stashes a crashed=True TurnRecord (the
        EventCollector's reduction of the events seen so far) on
        ``self.pending_turn`` for the orchestrator to drain. Best-effort: if the
        reduction itself fails we log and leave ``pending_turn=None`` so the typed
        exception's category still routes correctly. Iteration rollback happens in
        ``discard_pending_turn()``, per the Agent contract.
        """
        if st.finalized:
            return
        st.finalized = True

        # Force-close any tool calls that never received a result so they appear in
        # the record (and the event tree stays balanced).
        for tool_id, pending in st.commands.items():
            if tool_id in st.ended_tool_ids:
                continue
            telem = pending["telemetry"]
            if telem.result_status is None:
                telem.result_status = "unknown"
                self._log.warning("Tool %s (id=%s) ended without result", telem.tool_name, tool_id)
            if telem.duration_ms is None:
                telem.duration_ms = 0.0
            st.emit.on_event(
                ToolEndEvent(task_id=st.task_id, turn_id=st.turn_id, tool=telem, status=ToolEndStatus.UNRESOLVED)
            )
            st.ended_tool_ids.add(tool_id)

        # AgentEndStatus and TurnEndStatus share identical members; map by value.
        st.emit.on_event(
            TurnEndEvent(
                task_id=st.task_id, turn_id=st.turn_id, status=TurnEndStatus(status.value), tokens=st.token_usage
            )
        )

        st.emit.on_event(
            AgentEndEvent(
                task_id=st.task_id,
                status=status,
                usage=st.token_usage or TokenUsage(),
                iteration=st.iteration,
                user_input=st.user_input,
                # Prefer the host's authoritative result.response; fall back to the
                # transcript's merged final text so a delta-streamed answer (or a
                # crash/timeout partial) isn't truncated to its last fragment.
                agent_output=st.final_response or st.transcript.final_text(),
                model_used=st.model_used,
                assistant_turn_count=st.assistant_turn_count,
                # Per-generation transcript reconstructed from the event stream, with
                # token buckets zipped from the host's turnUsages (when present and
                # aligned). The collector reads this back verbatim into
                # TurnRecord.messages. On crash/timeout partials turn_usages is still
                # None, so messages carry content and timing but no token attribution.
                messages=st.transcript.build_messages(
                    turn_id=st.turn_id, model=st.model_used, turn_usages=st.turn_usages, log=self._log
                ),
                # num_turns is the cross-agent inner-loop turn count reports sum. The
                # delegate analog is the host's authoritative assistantStepCount,
                # already folded into assistant_turn_count. Left None on crash/timeout
                # partials (no result message arrived), matching ClaudeCodeAgent.
                num_turns=st.assistant_turn_count if not crashed else None,
                max_turns_exhausted=st.max_turns_exhausted,
                crashed=crashed,
                crash_reason=crash_reason,
                duration_seconds=time.monotonic() - st.turn_start,
            )
        )

        if crashed:
            try:
                self.pending_turn = st.collector.build_turn_record()
            except Exception:
                logger.exception("Failed to build partial turn record; continuing without partial")
                self.pending_turn = None

    async def kill(self) -> None:
        """Force-terminate the host subprocess. Fire-and-forget; safe at any time."""
        self.kill_sync()

    def kill_sync(self) -> None:
        """Synchronously SIGKILL the host subprocess.

        Invoked by :class:`ThreadedWatchdog` from its timer thread on task-level
        timeout, so this must not touch the event loop or await anything.
        ``asyncio.subprocess.Process.kill()`` ultimately calls
        ``os.kill``/``TerminateProcess``, which are thread-safe.
        """
        proc = self._process
        if proc is None or proc.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.kill()

    async def _force_kill_host(self) -> None:
        """Kill the host subprocess and await its exit. Idempotent."""
        self.kill_sync()
        if self._process is None:
            return
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self._process.wait(), timeout=5.0)

    @staticmethod
    def _parse_usage(raw: Any, model: str | None) -> TokenUsage | None:
        """Map the host's ``usage`` payload onto :class:`TokenUsage`.

        The host emits keys in coder_eval's pre-split naming
        (``input_tokens`` / ``output_tokens`` / ``cache_creation_input_tokens``
        / ``cache_read_input_tokens``) sourced from the Delegate framework's
        per-turn ``lastTurnUsage``. The host's ``input_tokens`` has always
        carried the fresh (uncached) prompt slice, so it maps onto
        ``uncached_input_tokens``; ``TokenUsage.input_tokens`` is now the
        derived total of all three prompt buckets and is never set directly.
        The Delegate SDK doesn't expose pricing, so
        ``total_cost_usd`` is computed locally from ``model`` via
        :func:`coder_eval.pricing.calculate_cost`; the backend's
        underscored id form is normalized to the table's hyphenated keys
        first. It stays ``None`` when the model is unknown or absent from the
        pricing table. Returns ``None``
        for missing, non-dict, or all-zero payloads so the caller can decide
        whether to keep any prior value (e.g. cached from an earlier turn).
        """
        if not isinstance(raw, dict):
            return None

        def _int(value: Any) -> int:
            return value if isinstance(value, int) and value >= 0 else 0

        usage = TokenUsage(
            uncached_input_tokens=_int(raw.get("input_tokens")),
            output_tokens=_int(raw.get("output_tokens")),
            cache_creation_input_tokens=_int(raw.get("cache_creation_input_tokens")),
            cache_read_input_tokens=_int(raw.get("cache_read_input_tokens")),
        )
        # An all-zero usage is indistinguishable from "framework didn't record
        # any usage for this turn yet" — surface it as None so the caller can
        # preserve a previous non-zero value rather than overwriting it.
        if usage.is_empty():
            return None
        if model:
            # The backend echoes underscored ids (``claude_sonnet_4_6``) while
            # the pricing table is keyed on the hyphenated form — normalize so
            # a priced model doesn't silently report total_cost_usd=None.
            usage.total_cost_usd = calculate_cost(
                model.replace("_", "-"),
                usage.uncached_input_tokens,
                usage.output_tokens,
                usage.cache_creation_input_tokens,
                usage.cache_read_input_tokens,
            )
        return usage

    async def _teardown_host(self) -> None:
        """Terminate the host subprocess and its drain tasks, then null them out.

        Idempotent and state-neutral (does NOT mark the agent stopped), so it is
        safe to call both from :meth:`stop` and at the top of :meth:`start` — the
        orchestrator re-invokes ``start`` on a retryable init failure WITHOUT an
        intervening ``stop``, so without this a retry would orphan the previous
        Node subprocess and leak both drain tasks.
        """
        try:
            if self._process is not None and self._process.returncode is None:
                try:
                    await self._send_command({"cmd": "destroy"})
                except Exception as e:
                    self._log.debug("Sending destroy to host failed: %s", e)

                try:
                    await asyncio.wait_for(self._process.wait(), timeout=_STOP_TIMEOUT_SEC)
                except TimeoutError:
                    self._log.warning("Delegate SDK host did not exit within %.1fs — killing", _STOP_TIMEOUT_SEC)
                    # Same guard as _force_kill_host: the host can exit on its own
                    # in the window between the wait_for timing out and this kill,
                    # and killing an already-reaped process raises.
                    with contextlib.suppress(ProcessLookupError, OSError):
                        self._process.kill()
                        await self._process.wait()
        finally:
            # Unconditional: the refresher owns a background task and a tempdir
            # holding a live bearer token, so a raise in the host-kill path above
            # must not leak them for the rest of the process.
            await self._cancel_drain_tasks()
            self._process = None
            # The refresher is per-start() (unlike _respawn_host, which keeps it so
            # the replacement host reads the same fresh file); a start() retry
            # builds a new one against the then-current env.
            if self._token_refresher is not None:
                refresher, self._token_refresher = self._token_refresher, None
                await refresher.stop()

    async def _cancel_drain_tasks(self) -> None:
        """Cancel and clear the stdout/stderr drain tasks. Idempotent."""
        for task_attr in ("_stderr_task", "_stdout_task"):
            task = getattr(self, task_attr, None)
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
                setattr(self, task_attr, None)

    async def stop(self) -> None:
        """Tear down the host subprocess cleanly."""
        await self._teardown_host()
        self._mark_stopped()

    def get_environment_info(self) -> dict[str, Any]:
        """Record the resolved Delegate routing so runs against different cloud
        envs (alpha/staging/production) — or a pinned localhost backend — are
        distinguishable in ``EvaluationResult.environment_info``.

        Only the *host* of any explicit ``BACKEND_URL``/``INTEROP_URL`` override
        is recorded (never the full URL), mirroring :class:`CodexAgent`, so an
        embedded credential can't leak into the run record.
        """
        # Spread the base first so the ``system_prompt_semantics`` run marker is
        # always present (dashboards read an absent marker as a pre-marker run).
        info: dict[str, Any] = {
            **super().get_environment_info(),
            "delegate_env": os.environ.get("DELEGATE_SDK_ENV") or "alpha",
        }
        if self.config.model:
            info["delegate_model"] = self.config.model
        if backend := os.environ.get("BACKEND_URL"):
            info["delegate_backend_url_host"] = urlparse(backend).hostname or ""
        if interop := os.environ.get("INTEROP_URL"):
            info["delegate_interop_url_host"] = urlparse(interop).hostname or ""
        return info

    def get_sdk_options(self) -> dict[str, Any] | None:
        """Return the options dict sent to the host on init (or ``None`` if not started)."""
        return self._init_options

    # -- Internals -----------------------------------------------------------

    def _build_init_options(self, *, env_path_prepend: list[str] | None = None) -> dict[str, Any]:
        """Translate :class:`DelegateSdkAgentConfig` fields to Delegate SDK init options.

        ``env_path_prepend`` is start()'s sandbox mock_path_dirs (see there).
        """
        options: dict[str, Any] = {}
        if self.config.model:
            options["model"] = self.config.model

        # Cloud env slug (alpha/staging/production) the host uses to compose
        # the backend URL from the saved auth's org/tenant slugs — matches
        # `npm start -- --env alpha` in basic.ts. Sourced from DELEGATE_SDK_ENV,
        # defaulting to "alpha". Host precedence: backendUrl > BACKEND_URL env
        # > env > localhost, so this is a no-op when an explicit backend URL is
        # also configured.
        options["env"] = os.environ.get("DELEGATE_SDK_ENV") or "alpha"

        # Task sandbox path. The host chdir()s into it and the SDK seeds the
        # shell tools' per-session default cwd from it, so commands run in the
        # sandbox without a per-call workingDirectory argument (and without a
        # working-directory prompt prefix — the orchestrator already injects
        # "Your working directory is: ..." into every prompt).
        if self.working_directory is not None:
            options["workingDirectory"] = str(self.working_directory)

        # Sandbox mock CLIs (SandboxConfig.mock_path_dirs, resolved by the
        # orchestrator). Forwarded only when non-empty so hosts that predate the
        # option — and the option-merge on the SDK side — see a clean config.
        # The host validates each dir exists and the SDK injects the composed
        # PATH into every shell command's environment.
        if env_path_prepend:
            options["shellPathPrepend"] = env_path_prepend
            self._log.debug("Forwarding shellPathPrepend=%s to the Delegate host", env_path_prepend)

        # Local wiki routing under the sandbox (workingDirectory). Forward only when
        # set: empty project_id keeps the per-session wiki dir; empty session_id lets
        # the SDK generate a fresh session (a pinned id skips createSession).
        if self.config.project_id:
            options["projectId"] = self.config.project_id
        if self.config.session_id:
            options["sessionId"] = self.config.session_id

        # config.plugins is list[SdkPluginConfig] (a TypedDict); the helper takes
        # list[dict[str, Any]] — identical at runtime, but list invariance trips
        # the checker. Same pattern as ClaudeCodeAgent's process_plugins call.
        bundled_skills = _resolve_bundled_skills_path(self.config.plugins)  # type: ignore[arg-type]
        if bundled_skills:
            options["enableSkills"] = True
            options["bundledSkillsPath"] = bundled_skills
        else:
            options["enableSkills"] = False

        # Reuse the cross-agent sdk_options surface to carry the reasoning-effort
        # tier (low/medium/high/xhigh). The host maps this onto useAppStore.effort,
        # which the ChatFramework already serialises as user_config.effort on every
        # chat request; backend/llm/effort.py then translates per provider — for
        # Kimi / Virtuoso (Fireworks) into the top-level reasoning_effort field
        # (low maps to a hard-disable-thinking extra_body payload upstream).
        #
        # DelegateSdkAgentConfig.sdk_options is a permissive dict[str, Any]; the
        # host only consumes `effort`. Other keys (Claude-only SDK options
        # carried by shared experiment YAMLs) are silently ignored.
        if (effort := self.config.sdk_options.get("effort")) is not None:
            options["effort"] = effort

        # Mirror the DELEGATE_STDIO_VERBOSE forwarding in start(): when
        # coder_eval runs at DEBUG (--verbose), turn on the SDK's own verbose so
        # DelegateAgent logs its setup (loaded skills, tool count, interop URL).
        if logger.isEnabledFor(logging.DEBUG):
            options["verbose"] = True

        # Runtime endpoints (env vars win, defaults match the SDK's own host).
        if backend := os.environ.get("BACKEND_URL"):
            options["backendUrl"] = backend
        if interop := os.environ.get("INTEROP_URL"):
            options["interopUrl"] = interop
        return options

    def _warn_unsupported_fields(self) -> None:
        """Emit a single warning listing :class:`DelegateSdkAgentConfig` fields the SDK can't honour."""
        unsupported: list[str] = []
        for field_name in _UNSUPPORTED_FIELDS:
            value = getattr(self.config, field_name, None)
            if value:  # falsy values (None, empty list/str) count as unset
                unsupported.append(field_name)
        if unsupported:
            self._log.warning(
                "DelegateSdkAgent does not support these AgentConfig fields — they will be ignored: %s",
                ", ".join(unsupported),
            )

    def _format_auth_init_error(self, host_message: str) -> str:
        """Build a fail-fast auth diagnostic from the host's init error.

        Distinguishes an *expired* saved login from an *absent* one and points at
        the env-specific login command, turning a confusing 40s retry-then-fail
        into a single actionable message. Raised as a non-retryable
        :class:`AgentConfigError` by ``start()``.
        """
        env = (self._init_options or {}).get("env") or os.environ.get("DELEGATE_SDK_ENV") or "alpha"
        return (
            f"Delegate SDK authentication failed during init: {host_message} "
            f"{_describe_saved_auth()} "
            f"Fix it by setting AUTH_TOKEN/TENANT_ID/ORG_ID, or run "
            f"`npx @uipath/delegate-cli login --env {env}` (writes ~/.aria/sdk-auth.json). "
            f"This error is non-retryable — failing fast instead of retrying."
        )

    async def _send_command(self, cmd: dict[str, Any]) -> None:
        """Write one JSON-Lines command to the host's stdin."""
        if self._process is None or self._process.stdin is None:
            raise RuntimeError("Host subprocess has no stdin")
        line = (json.dumps(cmd) + "\n").encode("utf-8")
        self._process.stdin.write(line)
        await self._process.stdin.drain()

    async def _read_line(self) -> dict[str, Any] | None:
        """Return the next JSON protocol message from the stdout drain queue,
        or ``None`` if the host has exited (EOF sentinel posted by
        :meth:`_drain_stdout`).

        Callers must handle ``None``: ``communicate()`` stashes a partial
        TurnRecord and raises :class:`AgentCrashError`; ``start()`` /
        ``_read_until()`` raise the bare exception.
        """
        return await self._stdout_queue.get()

    async def _drain_stdout(self, stdout: asyncio.StreamReader) -> None:
        """Continuously read the host's stdout, logging non-JSON lines and queuing protocol messages."""
        try:
            while True:
                try:
                    raw = await stdout.readline()
                except asyncio.LimitOverrunError as e:
                    # A single line exceeded _STREAM_READER_LIMIT_BYTES — the
                    # bytes are still buffered. Drain them so readline() can
                    # advance, log it, and treat the host as crashed (we
                    # can't reassemble the dropped JSON line, and silently
                    # continuing would leave communicate() blocked on the
                    # next result message). Posting the EOF sentinel routes
                    # this through the same AgentCrashError path as a real
                    # subprocess exit.
                    with contextlib.suppress(Exception):
                        await stdout.readexactly(e.consumed)
                    self._log.error(
                        "[delegate-sdk stdout] line exceeded %d-byte StreamReader limit; treating host as crashed",
                        _STREAM_READER_LIMIT_BYTES,
                    )
                    self._stderr_lines.append(
                        f"[coder_eval] stdout line exceeded {_STREAM_READER_LIMIT_BYTES}-byte limit",
                    )
                    raw = b""  # fall through to the EOF branch
                if not raw:
                    # EOF — wake the queue consumer FIRST, then settle the process /
                    # stderr drain. Posting after a multi-second wait would block any
                    # in-flight ``_read_line()`` for up to 3s, which under timeout
                    # pressure can flip a clean ``AgentCrashError`` into a
                    # ``TurnTimeoutError`` with worse diagnostics. The crash-message
                    # builder reads ``self._stderr_lines`` directly, so a slightly
                    # stale stderr tail is fine — better than a slow sentinel.
                    await self._stdout_queue.put(None)  # sentinel
                    if self._process is not None and self._process.returncode is None:
                        with contextlib.suppress(TimeoutError):
                            await asyncio.wait_for(self._process.wait(), timeout=2.0)
                    if self._stderr_task is not None and not self._stderr_task.done():
                        with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                            await asyncio.wait_for(self._stderr_task, timeout=1.0)
                    return
                text = raw.decode("utf-8", errors="replace").rstrip()
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    self._log.debug("[delegate-sdk stdout] %s", text)
                    continue
                if not isinstance(parsed, dict):
                    self._log.debug("[delegate-sdk stdout] non-object JSON skipped: %r", parsed)
                    continue
                await self._stdout_queue.put(parsed)
        except asyncio.CancelledError:
            raise
        except BaseException:
            # Any other unexpected failure in the drain loop would leave
            # communicate() blocked on _stdout_queue.get() forever. Post the
            # EOF sentinel so the consumer fails fast with AgentCrashError
            # instead of hanging until the threaded watchdog hard-kills the
            # subprocess (the original 3000s silent-hang bug).
            self._log.exception("[delegate-sdk] _drain_stdout crashed; posting EOF sentinel")
            with contextlib.suppress(Exception):
                await self._stdout_queue.put(None)
            raise

    def _build_crash_message(self) -> str:
        """Build a descriptive error message when the host process exits unexpectedly."""
        parts = ["Delegate SDK host crashed"]
        if self._process is not None and self._process.returncode is not None:
            parts.append(f"(exit code {self._process.returncode})")
        if self._stderr_lines:
            stderr_tail = "\n".join(list(self._stderr_lines)[-20:])
            parts.append(f"— stderr:\n{stderr_tail}")
        else:
            parts.append("— no stderr captured")
        return " ".join(parts)

    async def _read_until(self, accepted_types: tuple[str, ...]) -> dict[str, Any]:
        """Read host messages until one of the accepted types is seen.

        Raises :class:`AgentCrashError` if the host exits before producing
        an accepted message. Used during ``start()`` where there is no
        partial TurnRecord to stash.
        """
        while True:
            msg = await self._read_line()
            if msg is None:
                raise AgentCrashError(self._build_crash_message())
            if msg.get("type") in accepted_types:
                return msg
            self._log.debug("Ignoring host message while waiting for %s: %r", accepted_types, msg)

    @staticmethod
    def _find_pending_by_name(commands: dict[str, dict[str, Any]], tool_name: str) -> str | None:
        """Fall back to matching a tool result by name when no tool_id is supplied."""
        if not tool_name:
            return None
        for tool_id, pending in commands.items():
            if pending["telemetry"].tool_name == tool_name and pending["telemetry"].result_status is None:
                return tool_id
        return None

    async def _drain_stderr(self, stderr: asyncio.StreamReader) -> None:
        """Forward the host's stderr to our logger and store for error reporting.

        Mirrors ``_drain_stdout``'s resilience: a single verbose ``writeLine``
        echo can now be large, so an over-limit line is drained and dropped with a
        warning rather than letting ``LimitOverrunError`` kill this task. A dead
        ``_stderr_task`` would strand the EOF await in ``_drain_stdout`` (which
        joins it) and blank the crash-message stderr tail.
        """
        try:
            while True:
                try:
                    line = await stderr.readline()
                except asyncio.LimitOverrunError as e:
                    # Bytes are still buffered; drain them so readline() can
                    # advance, then drop the over-limit line and keep reading.
                    with contextlib.suppress(Exception):
                        await stderr.readexactly(e.consumed)
                    self._log.warning(
                        "[delegate-sdk stderr] line exceeded %d-byte StreamReader limit; dropped",
                        _STREAM_READER_LIMIT_BYTES,
                    )
                    continue
                if not line:
                    return
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    self._stderr_lines.append(text)
                    self._log.log(self._host_stderr_log_level, "[delegate-sdk] %s", text)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Never let an unexpected drain failure kill the task silently — the
            # stdout EOF path joins this task and the crash tail reads its buffer.
            # Exception (not BaseException): KeyboardInterrupt/SystemExit propagate.
            self._log.exception("[delegate-sdk] _drain_stderr crashed")
            return
