// Column definitions, surfaced as native `title` tooltips on table headers.
// Shared by the run-page task grid and the task-page message timeline so the two
// describe the same number the same way. Wording is page-neutral (no "this task"
// / "this call") because both surfaces read the same string.
export const TOKEN_COLUMN_HELP = {
    input: "Input tokens (uncached): fresh prompt input billed at the full input rate — the slice that was neither written to nor read from the prompt cache.",
    output: "Output tokens: text, code, tool arguments and reasoning the model generated.",
    cw: "Cache-write tokens: context newly written into the prompt cache (cache_creation_input_tokens).",
    cr: "Cache-read tokens: cached input re-billed on every later call (cache_read_input_tokens). Usually the dominant cost line.",
} satisfies Record<string, string>;
