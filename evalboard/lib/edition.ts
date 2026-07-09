// Build edition gate.
//
// "internal" enables UiPath-internal-only surfaces (the hosted dashboard:
// recent-runs index, watchlist, trends, activation, and UiPath branding).
// Anything else — including unset — is the public OSS edition, so a bare clone
// runs in OSS mode with no configuration. The hosted UiPath deploy opts in by
// setting EVALBOARD_EDITION=internal.
export const EDITION: "internal" | "oss" =
    process.env.EVALBOARD_EDITION === "internal" ? "internal" : "oss";

export const isInternal = EDITION === "internal";
