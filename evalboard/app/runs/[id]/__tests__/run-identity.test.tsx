import { describe, expect, test } from "vitest";
import { render, screen } from "@testing-library/react";
import { RunIdentity } from "../run-identity";

// The run page's numbers are only comparable across runs on the same harness and
// model, so getting this strip wrong (or silently omitting it) is what sends
// someone comparing a codex run's pass rate against a claude-code one.
describe("RunIdentity", () => {
    test("names the harness and the model", () => {
        render(
            <RunIdentity
                harness="codex"
                model="gpt-5.2-codex"
                modelCount={1}
            />,
        );
        expect(screen.getByText("Codex")).toBeInTheDocument();
        expect(screen.getByText("gpt-5.2-codex")).toBeInTheDocument();
    });

    test("uses the vendor logo, not just the raw harness id", () => {
        render(
            <RunIdentity
                harness="delegate-sdk"
                model="claude-sonnet-5"
                modelCount={1}
            />,
        );
        // The UiPath mark stands in for the Delegate SDK harness, which has no
        // file of its own under /harness.
        expect(
            screen.getByAltText("Delegate SDK · UiPath"),
        ).toBeInTheDocument();
        expect(screen.getByText("Delegate SDK")).toBeInTheDocument();
    });

    test("flags a multi-model run instead of claiming the dominant one", () => {
        render(
            <RunIdentity
                harness="claude-code"
                model="claude-sonnet-5"
                modelCount={3}
            />,
        );
        expect(screen.getByText("+2 more")).toBeInTheDocument();
    });

    test("says nothing extra on a single-model run", () => {
        render(
            <RunIdentity
                harness="claude-code"
                model="claude-sonnet-5"
                modelCount={1}
            />,
        );
        expect(screen.queryByText(/more/)).not.toBeInTheDocument();
    });

    test("renders each half independently when the other is missing", () => {
        const { rerender } = render(
            <RunIdentity harness="codex" model={null} modelCount={0} />,
        );
        expect(screen.getByText("Codex")).toBeInTheDocument();

        rerender(
            <RunIdentity harness={null} model="some-model" modelCount={1} />,
        );
        expect(screen.getByText("some-model")).toBeInTheDocument();
    });

    test("renders nothing for a legacy run that identifies neither", () => {
        const { container } = render(
            <RunIdentity harness={null} model={null} modelCount={0} />,
        );
        expect(container).toBeEmptyDOMElement();
    });
});
