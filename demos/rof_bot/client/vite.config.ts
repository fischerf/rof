import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  server: {
    port: 3000,
    proxy: {
      "/status": {
        target: "http://localhost:8080",
        changeOrigin: true,
      },
      "/runs": {
        target: "http://localhost:8080",
        changeOrigin: true,
      },
      "/config": {
        target: "http://localhost:8080",
        changeOrigin: true,
      },
      "/control": {
        target: "http://localhost:8080",
        changeOrigin: true,
      },
      "/metrics": {
        target: "http://localhost:8080",
        changeOrigin: true,
      },
      "/routing": {
        target: "http://localhost:8080",
        changeOrigin: true,
      },
      "/ws": {
        target: "ws://localhost:8080",
        changeOrigin: true,
        ws: true,
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: true,
    rollupOptions: {
      output: {
        manualChunks: {
          react: ["react", "react-dom"],
          router: ["react-router-dom"],
          charts: ["recharts"],
        },
      },
    },
  },
});
