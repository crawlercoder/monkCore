// Next.js configuration for the AI-agent operator UI.
//
// * ``rewrites`` forwards ``/api/*`` to the FastAPI backend so the
//   browser talks only to the Next origin (same-origin, no CORS
//   preflight). The target is configurable via ``API_PROXY_TARGET``
//   so dev and staging deployments can point at different hosts.
// * ``reactStrictMode`` is on to surface side-effect bugs early.
/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  async rewrites() {
    const target = process.env.API_PROXY_TARGET || "http://127.0.0.1:8000";
    return [
      {
        source: "/api/:path*",
        destination: `${target}/api/:path*`,
      },
    ];
  },
};

module.exports = nextConfig;
