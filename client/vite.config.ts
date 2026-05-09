import { defineConfig } from "vite";

export default defineConfig({
  server: {
    port: 5273,
    strictPort: true,
    host: true,
    proxy: {
      "/control": { target: "http://127.0.0.1:8800", changeOrigin: true },
      "/state": { target: "http://127.0.0.1:8800", changeOrigin: true },
      "/healthz": { target: "http://127.0.0.1:8800", changeOrigin: true },
      "/stream": { target: "ws://127.0.0.1:8800", ws: true, changeOrigin: true },
    },
  },
});
