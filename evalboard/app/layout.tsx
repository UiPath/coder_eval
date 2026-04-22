import type { ReactNode } from "react";
import Image from "next/image";
import { ADX_URL } from "@/lib/config";
import "./globals.css";

export const metadata = {
    title: "Flow Evalboard",
};

export default function RootLayout({ children }: { children: ReactNode }) {
    return (
        <html lang="en">
            <body className="min-h-screen bg-white text-gray-900 font-sans">
                <header className="border-b border-gray-200 px-8 py-3 flex items-center justify-between bg-white">
                    <a
                        href="/"
                        className="flex items-center gap-2 text-gray-900 font-semibold"
                    >
                        <Image
                            src="/uipath.png"
                            alt="UiPath"
                            width={28}
                            height={28}
                            priority
                        />
                        <span className="text-lg">Flow Evalboard</span>
                    </a>
                    <div className="flex items-center gap-4 text-sm">
                        <a
                            href={ADX_URL}
                            target="_blank"
                            rel="noreferrer"
                            className="inline-flex items-center gap-1 text-gray-700 hover:text-studio-blue"
                        >
                            ADX dashboard
                            <span className="text-xs">↗</span>
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
