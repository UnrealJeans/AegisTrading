/// <reference types="vitest/config" />
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

declare const process: { env: Record<string, string | undefined> };
const backend = process.env.VITE_BACKEND ?? "http://localhost:8000";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: { port: 5173, proxy: { "/api": { target: backend, ws: true, changeOrigin: false } } },
  test: { environment: "jsdom", globals: true, setupFiles: "./src/test-setup.ts", css: false },
});
