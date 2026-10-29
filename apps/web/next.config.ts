import type { NextConfig } from "next";

// The browser only talks to this origin; API calls are proxied to FastAPI, so the API needs no
// CORS configuration and the bearer token never goes to a third-party origin.
const api = process.env.FRAUD_API_URL ?? "http://127.0.0.1:8000";

const config: NextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  async rewrites() {
    return [
      { source: "/api/v1/:path*", destination: `${api}/api/v1/:path*` },
      { source: "/health/:path*", destination: `${api}/health/:path*` },
    ];
  },
};

export default config;
