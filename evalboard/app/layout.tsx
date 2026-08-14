import { Suspense, type ReactNode } from "react";
import Image from "next/image";
import { SearchBox } from "./_components/search-box";
import { isInternal } from "@/lib/edition";
import "./globals.css";

export const metadata = {
    title: "Coder Evalboard",
};

// Header nav. Every entry is internal-only today (the public OSS edition hides
// the whole block — see lib/edition.ts); the routes themselves stay reachable.
const NAV: readonly { href: string; label: string }[] = [
    { href: "/path-to-ga", label: "Path to GA" },
    { href: "/watchlist", label: "Watchlist" },
    { href: "/trends", label: "Trends" },
    // The Autopilot/aria suite, read from its own blob container — see lib/sources.ts.
    { href: "/scribe", label: "Scribe" },
];

export default function RootLayout({ children }: { children: ReactNode }) {
    return (
        <html lang="en">
            <body className="min-h-screen bg-white text-gray-900 font-sans">
                <header className="border-b border-gray-200 px-4 sm:px-8 py-3 flex flex-wrap items-center gap-x-6 gap-y-2 bg-white">
                    <a
                        href="/"
                        className="flex items-center gap-2 text-gray-900 font-semibold shrink-0"
                    >
                        <Image
                            src="/uipath.png"
                            alt="UiPath"
                            width={28}
                            height={28}
                            priority
                        />
                        <span className="text-lg">Coder Evalboard</span>
                    </a>
                    {/* Search drops to its own full-width row on phones (where it
                        would otherwise get crushed between the logo and nav). */}
                    <div className="order-last basis-full sm:order-none sm:basis-0 sm:flex-1 flex justify-center min-w-0">
                        <Suspense
                            fallback={
                                <div
                                    aria-hidden
                                    className="w-full max-w-xl h-9 rounded-md border border-gray-200 bg-gray-50"
                                />
                            }
                        >
                            <SearchBox className="w-full max-w-xl" />
                        </Suspense>
                    </div>
                    {isInternal && (
                        <nav className="ml-auto sm:ml-0 flex items-center gap-4 text-sm shrink-0">
                            {NAV.map((item) => (
                                <a
                                    key={item.href}
                                    href={item.href}
                                    className="text-gray-700 hover:text-studio-blue"
                                >
                                    {item.label}
                                </a>
                            ))}
                        </nav>
                    )}
                </header>
                <main className="px-4 sm:px-6 lg:px-8 py-6 max-w-[1400px] mx-auto">
                    {children}
                </main>
            </body>
        </html>
    );
}
