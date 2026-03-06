"""S2S OAuth token management for LLM Gateway authentication."""

import asyncio
import logging

import httpx

from .config import ProxyConfig


logger = logging.getLogger(__name__)


class TokenManager:
    """Manages S2S OAuth tokens for LLM Gateway authentication.

    Acquires tokens via client_credentials grant and caches them.
    Thread-safe via asyncio.Lock.
    """

    def __init__(self, config: ProxyConfig):
        self._config = config
        self._token: str | None = None
        self._lock = asyncio.Lock()

    async def get_token(self) -> str:
        """Get a valid access token, acquiring one if needed."""
        if self._token is None:
            async with self._lock:
                if self._token is None:
                    self._token = await self._acquire_token()
        return self._token

    async def refresh_token(self) -> str:
        """Force-refresh the token (called on 401 from gateway)."""
        async with self._lock:
            self._token = await self._acquire_token()
            return self._token

    async def _acquire_token(self) -> str:
        """Acquire a new S2S token from the identity endpoint."""
        base_url = self._config.llmgw_url.rstrip("/")
        token_url = f"{base_url}/identity_/connect/token"
        logger.debug("Acquiring S2S token from %s", token_url)

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                token_url,
                data={
                    "client_id": self._config.client_id,
                    "client_secret": self._config.client_secret,
                    "grant_type": "client_credentials",
                },
            )
            response.raise_for_status()
            token = response.json()["access_token"]
            logger.debug("S2S token acquired successfully")
            return token
