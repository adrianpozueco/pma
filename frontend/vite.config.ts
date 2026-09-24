import { defineConfig } from "vite";

// Dev/preview proxy to the local FastAPI backend (pm_agent.fast_api_app),
// so the browser calls same-origin /api/* and no CORS setup is needed.
const target = process.env.PMA_API_TARGET ?? "http://127.0.0.1:8000";
const proxy = {
  "/api": { target, changeOrigin: true, rewrite: (p: string) => p.replace(/^\/api/, "") },
};

export default defineConfig({
  server: { proxy },
  preview: { proxy },
});
