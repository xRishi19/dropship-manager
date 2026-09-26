import type { NextConfig } from "next";

// The FastAPI backend (uvicorn backend.app:create_app --factory --port 8000) serves /api.
const backend = process.env.BACKEND_URL ?? "http://127.0.0.1:8000";

const nextConfig: NextConfig = {
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${backend}/api/:path*` }];
  },
};

export default nextConfig;
