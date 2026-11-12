/**
 * The production UI is a static export that the FastAPI process serves.
 * Keeping it static means the page, the JSON API and the SSE stream all
 * share one origin -- and therefore one basic-auth challenge -- without
 * a Node server in the middle.
 *
 * ``npm run dev`` instead runs the regular Next.js server and proxies
 * ``/api`` to the Python process on port 8000.
 */
const isDev = process.env.NODE_ENV === "development";

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: false,
  agentRules: false,
  images: { unoptimized: true },
  ...(isDev
    ? {
        async rewrites() {
          return [
            {
              source: "/api/:path*",
              destination: "http://127.0.0.1:8000/api/:path*",
            },
          ];
        },
      }
    : {
        output: "export",
      }),
};

export default nextConfig;
