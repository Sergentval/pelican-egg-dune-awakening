import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { resolve } from "path";

// Output the built SPA into ../data/web/dist so the install script's
// existing `cp -r data/` step ships it to /home/container/data/web/dist/.
// admin-http.py serves whatever it finds there.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: resolve(__dirname, "..", "data", "web", "dist"),
    emptyOutDir: true,
    sourcemap: false,
    // Inline small assets to keep the served bundle compact and
    // free of asset 404s when the user accesses a deep link before
    // the SPA boots.
    assetsInlineLimit: 16 * 1024,
  },
  server: {
    // For local development against a running egg, proxy API + admin
    // calls back to the container's UI port via the wg-gt tunnel.
    proxy: {
      "/api": "http://10.99.0.2:8090",
      "/admin": "http://10.99.0.2:8090",
    },
  },
});
