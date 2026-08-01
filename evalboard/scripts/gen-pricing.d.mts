// Types for the plain-JS generator, so lib/__tests__/pricing-parity.test.ts can
// import its parser under `allowJs: false`. Declares only what that test
// consumes — everything else in gen-pricing.mjs is module-private, so this file
// stays a one-function surface rather than a second mirror to keep in sync.

export declare function parsePythonTable(): Record<
    string,
    [number, number, number, number]
>;

/** parsePythonTable + the coverage cross-check. Throws if a row was skipped. */
export declare function readTable(): Record<
    string,
    [number, number, number, number]
>;

/**
 * The subset of pricing.py the frontend table mirrors (everything except the
 * per-request-routed OpenRouter ids). Throws on a stale exclusion.
 */
export declare function mirroredTable(
    table: Record<string, [number, number, number, number]>,
): Record<string, [number, number, number, number]>;

/** The ids deliberately withheld from the frontend table. */
export declare function excludedModels(): string[];
