import { svelte } from "@sveltejs/vite-plugin-svelte";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "vite";

// No SvelteKit here on purpose. FastAPI already owns the routes, the auth and
// the read-model; a second server would only duplicate them. So this builds to
// static files that FastAPI serves from /assets, and the app is one bundle that
// picks a view from `location.pathname`.
export default defineConfig({
  plugins: [svelte(), tailwindcss()],
  resolve: {
    alias: {
      // `shadcn-svelte` resolves `aliases.components` through this, so a block's
      // alias-relative target (`nb/AppShell.svelte`) lands in src/lib/components.
      $lib: "/src/lib",
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
