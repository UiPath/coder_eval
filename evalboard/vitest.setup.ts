import "@testing-library/jest-dom/vitest";

// jsdom has no ResizeObserver, and recharts' ResponsiveContainer constructs one
// on mount — without this stub any test that renders a chart throws. jsdom also
// reports every element as 0x0, which makes recharts log a "width(0) and
// height(0)" warning and skip drawing, so the stub reports a fixed size instead.
// Chart tests assert on headings, legends and tab state, never on plotted
// geometry, so the exact numbers only need to be non-zero.
if (!("ResizeObserver" in globalThis)) {
    const RECT = { width: 800, height: 300, top: 0, left: 0, bottom: 300, right: 800, x: 0, y: 0 };
    globalThis.ResizeObserver = class {
        constructor(private cb: ResizeObserverCallback) {}
        observe(target: Element) {
            this.cb(
                [{ target, contentRect: RECT } as unknown as ResizeObserverEntry],
                this as unknown as ResizeObserver,
            );
        }
        unobserve() {}
        disconnect() {}
    } as unknown as typeof ResizeObserver;
}
