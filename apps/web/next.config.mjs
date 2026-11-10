/** @type {import('next').NextConfig} */
const API_ORIGIN = process.env.BACKEND_ORIGIN ?? "http://localhost:8000";

const nextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  typescript: {
    ignoreBuildErrors: false,
  },
  eslint: {
    ignoreDuringBuilds: true,
  },
  // framer-motion is ESM-only; transpiling it keeps the dev server from emitting
  // a broken `./vendor-chunks/framer-motion.js` reference when it is pulled in
  // from a client boundary (the `/play/[nodeId]` arena imports it heavily).
  transpilePackages: ["framer-motion"],
  experimental: {
    // Rewrite barrel imports into per-module paths: smaller chunks, fewer
    // vendor-chunk splits, and less chance of a stale chunk going missing.
    optimizePackageImports: ["framer-motion", "lucide-react"],
  },
  env: {
    NEXT_PUBLIC_API_URL: process.env.NEXT_PUBLIC_API_URL ?? API_ORIGIN,
  },
  webpack: (config) => {
    // Stale dev vendor chunks (framer-motion / @opentelemetry) surface as
    // MODULE_NOT_FOUND at runtime; keep the infra logs quiet so the real error
    // is the one the developer sees.
    config.infrastructureLogging = { level: "error" };
    return config;
  },
  async rewrites() {
    return [
      {
        source: "/backend/:path*",
        destination: `${API_ORIGIN}/:path*`,
      },
    ];
  },
  async headers() {
    return [
      {
        source: "/api/:path*",
        headers: [{ key: "Cache-Control", value: "no-store" }],
      },
    ];
  },
};

export default nextConfig;
