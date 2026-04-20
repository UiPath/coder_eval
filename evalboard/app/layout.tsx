import type { ReactNode } from "react";
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
                        className="flex items-baseline gap-1.5 text-gray-900 font-semibold"
                    >
                        <span className="text-uipath-orange font-bold">
                            Ui
                        </span>
                        <span className="text-lg">Flow Evalboard</span>
                    </a>
                    <span className="text-gray-500 text-sm">
                        local dashboard
                    </span>
                </header>
                <main className="px-8 py-6 max-w-[1400px] mx-auto">
                    {children}
                </main>
            </body>
        </html>
    );
}
