import { describe, expect, test } from "vitest";
import { render, screen } from "@testing-library/react";
import type { ConversationTurn } from "@/lib/runs";
import { ConversationSection } from "../_sections";

function makeTurn(overrides: Partial<ConversationTurn> = {}): ConversationTurn {
    return {
        role: "USER",
        turn: 1,
        metadata: null,
        text: "hello",
        ...overrides,
    };
}

describe("ConversationSection", () => {
    test("renders both turns' text and roles", () => {
        render(
            <ConversationSection
                turns={[
                    makeTurn({ role: "USER", text: "please write fizzbuzz" }),
                    makeTurn({ role: "AGENT", text: "here is the code" }),
                ]}
            />,
        );
        expect(screen.getByText("please write fizzbuzz")).toBeInTheDocument();
        expect(screen.getByText("here is the code")).toBeInTheDocument();
        expect(screen.getByText(/USER · turn 1/)).toBeInTheDocument();
        expect(screen.getByText(/AGENT · turn 1/)).toBeInTheDocument();
    });

    test("shows metadata in the turn header when present", () => {
        render(
            <ConversationSection
                turns={[makeTurn({ turn: 2, metadata: "stop_token" })]}
            />,
        );
        expect(screen.getByText(/turn 2 · stop_token/)).toBeInTheDocument();
    });
});
