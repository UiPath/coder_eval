// Leaf constants for artifact classification. Deliberately kept out of
// lib/runs.ts (which imports ./blob → @azure/* → node:crypto) so that client
// components — e.g. the artifact KindChip in _chips.tsx, now reachable from the
// client-side message timeline — can import them without dragging the
// server-only blob/azure chain into the browser bundle.

// Project deliverables float to the top of the artifact list and get a colored
// chip. Single source of truth so "float to top" (artifactRank) and "color the
// chip" (KindChip) can't drift.
export const DELIVERABLE_KINDS = new Set([
    "flow",
    "bpmn",
    "uipx",
    "uiproj",
    "xaml",
]);

export const DELIVERABLE_NAMES = new Set(["sdd.md", "recommendation.json"]);
