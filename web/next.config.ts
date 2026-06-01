import type { NextConfig } from "next";

// CSP — Kimi audit 2026-05-17 P2-3. Read-only public dashboard, нет auth,
// нет user-gen content, нет form-сабмитов наружу. Цель: блокировать любой
// third-party script-injection через сломанную dependency или transitive
// supply-chain compromise.
//
// `unsafe-inline` script-src нужен Next 15 для hydration boot script
// (`window.__NEXT_DATA__` идёт через inline <script>), миграция на nonce
// требует middleware refactor — отложено. `unsafe-eval` нужен Recharts
// (его scale builders компилируют функции). Эти два relaxations означают
// что CSP не защищает от XSS-через-injected-content, но это всё равно
// невозможно тут (нет user input в DOM, всё через React text nodes).
const cspDirectives = [
  "default-src 'self'",
  "script-src 'self' 'unsafe-inline' 'unsafe-eval'",
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data: blob: https://*.public.blob.vercel-storage.com",
  "font-src 'self' data:",
  "connect-src 'self'",
  "frame-ancestors 'none'",
  "base-uri 'self'",
  "form-action 'self'",
  "object-src 'none'",
  "upgrade-insecure-requests",
].join("; ");

const nextConfig: NextConfig = {
  // DuckDB native binaries must not be bundled by Webpack.
  webpack: (config, { isServer, nextRuntime }) => {
    if (isServer && nextRuntime === "nodejs") {
      config.externals = config.externals || [];
      const externals = ["@duckdb/node-api"];
      if (Array.isArray(config.externals)) {
        config.externals.push(...externals);
      } else {
        config.externals = [config.externals, ...externals];
      }
    }
    return config;
  },
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "Content-Security-Policy", value: cspDirectives },
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
          { key: "X-Frame-Options", value: "DENY" },
          {
            key: "Permissions-Policy",
            value: "camera=(), microphone=(), geolocation=(), interest-cohort=()",
          },
        ],
      },
    ];
  },
};

export default nextConfig;
