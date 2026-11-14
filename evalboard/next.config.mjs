/** @type {import('next').NextConfig} */
const nextConfig = {
    output: "standalone",
    async redirects() {
        return [
            { source: "/scribe", destination: "/series/scribe", permanent: false },
        ];
    },
};
export default nextConfig;
