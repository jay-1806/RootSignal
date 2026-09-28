import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The dev server proxies /api to the backend, so the browser never needs CORS.
// In docker-compose API_URL is http://backend:8000.
export default defineConfig({
  plugins: [react()],
  server: {
    host: true,
    port: 5173,
    proxy: {
      "/api": process.env.API_URL ?? "http://localhost:8000",
    },
  },
});
