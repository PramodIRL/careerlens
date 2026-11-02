import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  async rewrites() {
    const api = process.env.API_PROXY_TARGET;
    return api
      ? [{ source: "/api/v1/:path*", destination: `${api}/api/v1/:path*` }]
      : [];
  },
};

export default nextConfig;