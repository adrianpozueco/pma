import { defineConfig } from "vite";

// Dev/preview proxy to the local FastAPI backend (pm_agent.fast_api_app),
// so the browser calls same-origin /api/* and no CORS setup is needed.
const target = process.env.PMA_API_TARGET ?? "http://127.0.0.1:8000";
const proxy = {
  "/api": {
    target,
    changeOrigin: true,
    rewrite: (p: string) => p.replace(/^\/api/, ""),
    // ADK's app returns 403 for POSTs whose Origin is not in ALLOW_ORIGINS.
    // The browser only ever talks to this same-origin proxy, so forward the
    // request as a server-to-server call without the browser's Origin.
    configure: (server: { on: (e: "proxyReq", cb: (req: { removeHeader(n: string): void }) => void) => void }) => {
      server.on("proxyReq", (req) => req.removeHeader("origin"));
    },
  },
};

export default defineConfig({
  // The Cloud Run image serves the build under /ui/ from the FastAPI app.
  base: process.env.VITE_BASE ?? "/",
  server: { proxy },
  preview: { proxy },
});
