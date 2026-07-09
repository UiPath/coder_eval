import { Suspense, type ReactNode } from "react";
import Image from "next/image";
import { SearchBox } from "./_components/search-box";
import { isInternal } from "@/lib/edition";
import "./globals.css";

export const metadata = {
    title: "Coder Evalboard",
};

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
                    {/* Watchlist + Trends are internal-only surfaces; the public
                        OSS edition hides these nav links (see lib/edition.ts). The
                        routes themselves are left intact. */}
                    {isInternal && (
                        <div className="ml-auto sm:ml-0 flex items-center gap-4 text-sm shrink-0">
                            <a
                                href="/path-to-ga"
                                className="text-gray-700 hover:text-studio-blue"
                            >
                                Path to GA
                            </a>
                            <a
                                href="/watchlist"
                                className="text-gray-700 hover:text-studio-blue"
                            >
                                Watchlist
                            </a>
                            <a
                                href="/trends"
                                className="text-gray-700 hover:text-studio-blue"
                            >
                                Trends
                            </a>
                        </div>
                    )}
                </header>
                <main className="px-4 sm:px-6 lg:px-8 py-6 max-w-[1400px] mx-auto">
                    {children}
                </main>
            </body>
        </html>
    );
}
