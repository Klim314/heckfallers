import { defineConfig } from "vite";

const httpTarget = process.env.HEXA_PROXY_TARGET ?? "http://127.0.0.1:8800";
const wsTarget = httpTarget.replace(/^http/, "ws");

export default defineConfig({
  server: {
    port: 5273,
    strictPort: true,
    host: true,
    proxy: {
      "/control": { target: httpTarget, changeOrigin: true },
      "/state": { target: httpTarget, changeOrigin: true },
      "/healthz": { target: httpTarget, changeOrigin: true },
      "/stream": { target: wsTarget, ws: true, changeOrigin: true },
    },
  },
});
