"""Keep a Delegate-host token file fresh by re-minting LLMGW S2S tokens.

Closes the auth gap on runs that outlive the ~1h S2S token TTL: the adapter
strips the ``LLMGW_*`` client-credentials pair from the host env on purpose
(the host env is inherited by the shells its interop spawns — i.e. by the code
under test), which also disables the delegate-stdio host's own S2S
self-refresh. Without an external refresher writing ``DELEGATE_AUTH_TOKEN_FILE``,
the host keeps its start-up ``AUTH_TOKEN`` forever and every request past the
TTL dies with ``401 Invalid token: Signature has expired``.

:class:`S2sTokenFileRefresher` is that external refresher, run inside the
adapter's own process: it holds the ``LLMGW_*`` pair privately (never in the
host env), re-mints a ``service.internal`` token before each expiry, and
publishes it through a token FILE the host already knows how to consume — the
host re-reads the file at init, at every turn, and on its own refresh timer.

Guard rails:

* It only activates when the run's inherited ``AUTH_TOKEN`` was itself minted
  from the same LLMGW client (matching ``client_id`` claim). Some Delegate
  backends reject ``client_credentials`` tokens outright ("Invalid user
  token"), so a fresher S2S token must never be forced onto a run that
  authenticated another way.
* An externally configured token file (``DELEGATE_AUTH_TOKEN_FILE`` /
  ``AUTH_TOKEN_FILE``) always wins — e.g. the nightly keeps a USER token fresh
  there, and this refresher must not fight it. Pointing
  ``DELEGATE_AUTH_TOKEN_FILE`` at a file of your own is therefore also the way
  to switch this refresher off.
* The token is minted over ``https`` only — the request body carries
  ``LLMGW_CLIENT_SECRET``, so a plaintext ``LLMGW_URL`` is refused rather than
  silently downgraded.
* The refresher is best-effort by construction: every mint/write failure
  degrades to "keep serving the previous token and retry", never to an
  exception escaping into the caller's ``start()``.

Note that the token FILE (not the secret) is reachable by the code under test,
since its path is published on the host env the interop shells inherit. That is
a narrowing of the pre-existing ``AUTH_TOKEN`` exposure — the secret that mints
tokens stays adapter-side — but it does mean an exfiltrated token stays fresh
for the life of the run rather than dying at the first TTL boundary.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import http.client
import json
import logging
import os
import shutil
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


#: The eval's LLM-Gateway S2S credentials — held by the adapter, never the host.
GATEWAY_S2S_ENV_VARS = ("LLMGW_CLIENT_ID", "LLMGW_CLIENT_SECRET", "LLMGW_URL")

#: Env names the delegate-stdio host resolves a token file from. Both are set
#: on the host env: current bundles read the first, older ones only the second.
TOKEN_FILE_ENV_VARS = ("DELEGATE_AUTH_TOKEN_FILE", "AUTH_TOKEN_FILE")

_MINT_TIMEOUT_SECONDS = 30.0
#: UiPath's Cloudflare WAF bans urllib's default ``Python-urllib/3.x`` UA
#: (403, "error code: 1010") before the request reaches the IdP; any
#: descriptive UA passes.
_USER_AGENT = "coder-eval-uipath-s2s-refresher/1.0"
#: Mirror the host's refresh scheduling (delegate-stdio REFRESH_* constants) so
#: the file is rewritten just before the host itself would go looking for it.
_REFRESH_LEAD_SECONDS = 300.0
_RETRY_SECONDS = 60.0
_MIN_DELAY_SECONDS = 60.0
#: Cadence when a token carries no decodable ``exp`` (matches the pipeline's
#: uip-CLI refresher interval — comfortably inside the observed 3600s TTL).
_FALLBACK_INTERVAL_SECONDS = 3000.0


def decode_jwt_claims(token: str) -> dict[str, object] | None:
    """Decode a JWT's payload segment without verifying the signature.

    Returns:
        The claims dict, or None for malformed tokens. Claims are only
        consulted for gating and refresh scheduling, never for auth.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1]
    try:
        decoded = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        claims = json.loads(decoded)
    except ValueError:  # binascii.Error / UnicodeDecodeError / JSONDecodeError all subclass it
        return None
    return claims if isinstance(claims, dict) else None


def _token_exp_epoch_seconds(token: str) -> float | None:
    claims = decode_jwt_claims(token)
    exp = claims.get("exp") if claims else None
    return float(exp) if isinstance(exp, (int, float)) else None


@dataclass(frozen=True, slots=True)
class _S2sCreds:
    token_url: str
    client_id: str
    client_secret: str


def _read_creds(env: Mapping[str, str]) -> _S2sCreds | None:
    """Build the credentials bundle from ``LLMGW_*``, or None when incomplete.

    ``LLMGW_URL`` may arrive as a bare host or with a gateway path appended;
    the Identity Server is always mounted at the origin, so resolve against
    the origin the way the host's own S2S source does.

    The scheme is pinned to ``https``: :func:`_mint_s2s_token` puts
    ``LLMGW_CLIENT_SECRET`` in the request body, and a captured client_secret
    mints ``service.internal`` tokens indefinitely — so a plaintext or
    non-HTTP(S) ``LLMGW_URL`` declines the refresher rather than shipping the
    secret in the clear. This also constrains the URL that reaches
    ``urllib.request.urlopen`` (bandit B310).
    """
    client_id = env.get("LLMGW_CLIENT_ID")
    client_secret = env.get("LLMGW_CLIENT_SECRET")
    base_url = env.get("LLMGW_URL")
    if not client_id or not client_secret or not base_url:
        return None
    split = urllib.parse.urlsplit(base_url)
    if split.scheme != "https" or not split.netloc:
        return None
    token_url = f"https://{split.netloc}/identity_/connect/token"
    return _S2sCreds(token_url=token_url, client_id=client_id, client_secret=client_secret)


def _mint_s2s_token(creds: _S2sCreds) -> str:
    """POST the client_credentials grant and return the access token.

    Blocking (urllib) on purpose — callers run it via ``asyncio.to_thread``.

    Raises:
        RuntimeError: The IdP refused the grant or returned no access_token.
    """
    body = urllib.parse.urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
        }
    ).encode("ascii")
    request = urllib.request.Request(
        creds.token_url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": _USER_AGENT},
        method="POST",
    )
    try:
        # B310: _read_creds pins the scheme to https, so no file:/custom scheme can reach here.
        with urllib.request.urlopen(request, timeout=_MINT_TIMEOUT_SECONDS) as response:  # nosec B310
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as cause:
        # The body distinguishes an IdP refusal ({"error":"invalid_client"})
        # from a WAF block ("error code: 1010") — never echo the request body.
        cause_detail = ""
        with contextlib.suppress(Exception):
            cause_detail = cause.read().decode("utf-8", errors="replace")[:200].strip()
        raise RuntimeError(
            f"Token endpoint {creds.token_url} rejected client_credentials: {cause}"
            + (f" | body: {cause_detail}" if cause_detail else "")
        ) from cause
    except (OSError, http.client.HTTPException, ValueError) as cause:
        # Covers more than urllib.error.URLError on purpose: only ``h.request()``
        # is wrapped into a URLError inside urllib, so a front door that drops a
        # keep-alive connection before the status line raises
        # http.client.RemoteDisconnected straight out of urlopen, and
        # response.read() can raise IncompleteRead / ssl.SSLError. Normalising
        # them here is what makes the documented RuntimeError contract true.
        raise RuntimeError(f"Token endpoint {creds.token_url} rejected client_credentials: {cause}") from cause
    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise RuntimeError(f"Token endpoint {creds.token_url} returned no access_token")
    return access_token


class S2sTokenFileRefresher:
    """Keeps a raw-token file fresh for the Delegate host to re-read.

    Lifecycle: :meth:`maybe_create` gates activation, :meth:`start` mints the
    first token, writes the file, and arms the background refresh task,
    :meth:`stop` cancels the task and removes the file. One instance per
    adapter ``start()``.
    """

    def __init__(
        self,
        creds: _S2sCreds,
        inherited_token: str,
        log: logging.Logger | logging.LoggerAdapter,  # type: ignore[type-arg]
    ) -> None:
        self._creds = creds
        self._inherited_token = inherited_token
        self._log = log
        self._dir: Path | None = None
        self._task: asyncio.Task[None] | None = None

    @classmethod
    def maybe_create(
        cls,
        env: Mapping[str, str],
        log: logging.Logger | logging.LoggerAdapter,  # type: ignore[type-arg]
    ) -> S2sTokenFileRefresher | None:
        """Create a refresher when this run can safely self-refresh, else None.

        Activation requires all of:

        * the ``LLMGW_*`` client-credentials triple in ``env``;
        * no externally configured token file (that refresher owns freshness);
        * an inherited ``AUTH_TOKEN`` whose ``client_id`` claim matches
          ``LLMGW_CLIENT_ID`` — proof the run already authenticates with
          tokens minted from this exact client, so a re-mint yields the same
          kind of token the backend demonstrably accepts.

        ``env`` is the ADAPTER's own env: it is the only place the ``LLMGW_*``
        pair can still be read from, because the caller strips that pair off the
        host env before getting here.
        """
        creds = _read_creds(env)
        if creds is None:
            return None
        if configured := [name for name in TOKEN_FILE_ENV_VARS if env.get(name)]:
            log.debug(
                "S2S token-file refresher: %s already configured — external refresher owns freshness", configured[0]
            )
            return None
        inherited = env.get("AUTH_TOKEN")
        if not inherited:
            log.debug("S2S token-file refresher: no AUTH_TOKEN in env — nothing to keep fresh")
            return None
        claims = decode_jwt_claims(inherited)
        if claims is None or claims.get("client_id") != creds.client_id:
            log.info(
                "S2S token-file refresher: inherited AUTH_TOKEN was not minted by this LLMGW client "
                + "(client_id mismatch) — leaving token freshness alone"
            )
            return None
        return cls(creds, inherited, log)

    @property
    def token_file(self) -> str:
        """Absolute path of the token file (available after :meth:`start`)."""
        if self._dir is None:
            raise RuntimeError("S2sTokenFileRefresher.start() has not run")
        return str(self._dir / "delegate-auth-token")

    async def start(self) -> str:
        """Mint the first token, write the file, arm the refresh task.

        Falls back to the inherited token when the initial mint fails — the
        background task then keeps retrying, and the host re-reads the file
        every turn, so a late first mint still lands.

        Returns:
            The token-file path to publish via :data:`TOKEN_FILE_ENV_VARS`.
        """
        self._dir = Path(tempfile.mkdtemp(prefix="delegate-s2s-token-"))
        try:
            token = await asyncio.to_thread(_mint_s2s_token, self._creds)
            self._log.debug("S2S token-file refresher: initial mint OK (length=%d)", len(token))
        except Exception as error:
            # Broad on purpose: the refresher is an enhancement, so ANY initial
            # mint failure has to degrade to "serve the inherited token" rather
            # than propagate into the caller's start().
            self._log.warning(
                "S2S token-file refresher: initial mint failed (%s) — seeding the file with the inherited token", error
            )
            token = self._inherited_token
        self._write_token(token)
        self._task = asyncio.create_task(self._refresh_loop(token))
        return self.token_file

    async def stop(self) -> None:
        """Cancel the refresh task and remove the token file. Idempotent."""
        if self._task is not None:
            task, self._task = self._task, None
            task.cancel()
            # asyncio.wait (unlike ``await task``) reports completion without
            # re-raising, so the cancellation and a pre-existing failure can be
            # told apart below instead of one masking the other.
            await asyncio.wait({task})
            # A refresh loop that died on its own is the silent version of the
            # 401-past-TTL failure this class exists to prevent, so surface it
            # instead of letting the cancel/await swallow the traceback.
            if not task.cancelled() and (error := task.exception()) is not None:
                self._log.error("S2S token-file refresher: refresh loop had died — %r", error, exc_info=error)
        if self._dir is not None:
            await asyncio.to_thread(shutil.rmtree, self._dir, ignore_errors=True)
            self._dir = None

    def _write_token(self, token: str) -> None:
        """Atomically replace the token file (raw single-token format)."""
        if self._dir is None:
            raise RuntimeError("S2sTokenFileRefresher.start() has not run")
        staging = self._dir / "delegate-auth-token.tmp"
        staging.write_text(token, encoding="utf-8")
        os.replace(staging, self.token_file)

    def _delay_until_refresh(self, token: str) -> float:
        exp = _token_exp_epoch_seconds(token)
        if exp is None:
            return _FALLBACK_INTERVAL_SECONDS
        return max(_MIN_DELAY_SECONDS, exp - time.time() - _REFRESH_LEAD_SECONDS)

    async def _refresh_loop(self, current_token: str) -> None:
        delay = self._delay_until_refresh(current_token)
        while True:
            await asyncio.sleep(delay)
            try:
                token = await asyncio.to_thread(_mint_s2s_token, self._creds)
                self._write_token(token)
            except Exception as error:
                # Broad, and covering the write as well as the mint: any escape
                # here ends the task for good, and the run then dies at the TTL
                # with the exact `401 Signature has expired` this class exists
                # to prevent. The previous (stale but still valid) token stays
                # on disk, so retrying is always better than giving up.
                self._log.warning(
                    "S2S token-file refresher: refresh failed (%s) — retrying in %.0fs", error, _RETRY_SECONDS
                )
                delay = _RETRY_SECONDS
                continue
            delay = self._delay_until_refresh(token)
            self._log.debug("S2S token-file refresher: fresh token written; next refresh in %.0fs", delay)
