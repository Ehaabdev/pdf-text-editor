import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    // The Python server runs on 8000 during development.
    proxy: { "/api": "http://127.0.0.1:8000" },
  },
});
