import react from "@vitejs/plugin-react"
import tailwindcss from "@tailwindcss/vite"
import { defineConfig } from "vite"

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      "/api/auth": process.env.AUTH_DEV_UPSTREAM ?? "http://127.0.0.1:4210",
    },
  },
  build: {
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (id.includes("/node_modules/@assistant-ui/") || id.includes("/node_modules/assistant-")) {
            return "assistant-ui"
          }
          if (id.includes("/node_modules/react") || id.includes("/node_modules/scheduler")) {
            return "react"
          }
        },
      },
    },
  },
  resolve: {
    alias: {
      "@": new URL("./src", import.meta.url).pathname,
    },
  },
})
