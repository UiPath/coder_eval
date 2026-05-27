import { Suspense, type ReactNode } from "react";
import Image from "next/image";
import { SearchBox } from "./_components/search-box";
import "./globals.css";

export const metadata = {
    title: "Coder Evalboard",
};

export default function RootLayout({ children }: { children: ReactNode }) {
    return (
        <html lang="en">
            <body className="min-h-screen bg-white text-gray-900 font-sans">
                <header className="border-b border-gray-200 px-8 py-3 flex items-center gap-6 bg-white">
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
                    <div className="flex-1 flex justify-center min-w-0">
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
                    <div className="flex items-center gap-4 text-sm shrink-0">
                        <a
                            href="/trends"
                            className="text-gray-700 hover:text-studio-blue"
                        >
                            Trends
                        </a>
                    </div>
                </header>
                <main className="px-8 py-6 max-w-[1400px] mx-auto">
                    {children}
                </main>
            </body>
        </html>
    );
}
