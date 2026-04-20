import type { Config } from "tailwindcss";

export default {
    content: ["./app/**/*.{ts,tsx}"],
    theme: {
        extend: {
            colors: {
                studio: {
                    blue: "#1976D2",
                    "blue-hover": "#1565C0",
                },
                uipath: {
                    orange: "#FA4616",
                },
            },
            fontFamily: {
                sans: [
                    "-apple-system",
                    "BlinkMacSystemFont",
                    "Segoe UI",
                    "Roboto",
                    "Helvetica Neue",
                    "Arial",
                    "sans-serif",
                ],
                mono: [
                    "ui-monospace",
                    "SFMono-Regular",
                    "Menlo",
                    "monospace",
                ],
            },
        },
    },
    plugins: [],
} satisfies Config;
