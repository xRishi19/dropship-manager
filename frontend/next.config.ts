import type { NextConfig } from "next";

// The FastAPI backend (uvicorn backend.app:create_app --factory --port 8000) serves /api.
const backend = process.env.BACKEND_URL ?? "http://127.0.0.1:8000";

const nextConfig: NextConfig = {
  /**
   * Proxy frontend /api requests to the configured FastAPI backend, defaulting to the local backend URL.
   *
   * @remarks Takes no arguments.
   * @returns Promise resolving to the Next.js rewrite rule preserving the remaining request path.
   */
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${backend}/api/:path*` }];
  },
};

export default nextConfig;
