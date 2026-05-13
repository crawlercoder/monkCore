/**
 * Legacy SPA — prefer the Next.js app in ``../web`` for a single UI (one build,
 * one dev server). This file remains for older workflows.
 */
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// `VITE_API_BASE_URL` wires the app to the FastAPI backend. In dev we
// expose it as `import.meta.env.VITE_API_BASE_URL` (see `src/api.ts`)
// and fall back to localhost:8000. The dev server runs on 9000, which
// is already allow-listed in the backend's default CORS_ORIGINS.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 9000,
    strictPort: true,
    // Browser talks to the Vite origin only; this avoids CORS and many
    // ``ERR_CONNECTION_RESET`` cases (IPv4/IPv6 ``localhost`` quirks, reload).
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: true,
  },
});
