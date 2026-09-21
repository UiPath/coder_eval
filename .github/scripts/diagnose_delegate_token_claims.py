"""Print MATCH/NO MATCH verdicts for a ROPC-minted Delegate token's org/tenant claims.

Used by the `delegate-live-tests` job in ../workflows/pr-checks.yml. Takes the
token's base64url-encoded JWT payload (argv[1]; never the full token) and
compares its account/org and tenant claims against the DELEGATE_ORG_ID /
DELEGATE_TENANT_ID secrets (read from the environment, never printed
directly -- GitHub Actions auto-masks any exact secret value in log output,
so a raw side-by-side print would be unreliable for a human to eyeball).
"""

from __future__ import annotations

import base64
import json
import os
import sys


ORG_CANDIDATES = ["account_id", "accountId", "organizationId", "organization_id"]
TENANT_CANDIDATES = ["prt_id", "tenant_id", "tenantId"]


def _decode_claims(payload: str) -> dict[str, object]:
    padded = payload + "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(padded))


def _print_verdict(claims: dict[str, object], candidates: list[str], expected: str, label: str) -> None:
    found = {k: claims[k] for k in candidates if k in claims}
    if not found:
        print(f"{label}: token carries none of {candidates} -- cannot verify")
        return
    for key, value in found.items():
        verdict = "MATCH" if value == expected else "NO MATCH"
        print(f"{label} claim {key}: {verdict} against DELEGATE_{label}_ID")


def main() -> None:
    claims = _decode_claims(sys.argv[1])
    _print_verdict(claims, ORG_CANDIDATES, os.environ["EXPECTED_ORG_ID"], "ORG")
    _print_verdict(claims, TENANT_CANDIDATES, os.environ["EXPECTED_TENANT_ID"], "TENANT")
    print(f"All claim keys present on token (names only, no values): {sorted(claims.keys())}")


if __name__ == "__main__":
    main()
