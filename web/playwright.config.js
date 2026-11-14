import { defineConfig } from "@playwright/test";

// No Python in this suite. The server's contract -- facets, precedence, the
// plan-type view -- is pytest's job (`tests/test_views.py`,
// `tests/test_plan_types_page.py`); this suite answers the other half: given
// that JSON, does the screen read. So it serves the built bundle from
// `vite preview` and fulfils `/ui/*.json` from e2e/fixtures.js.
const port = Number(process.env.E2E_PORT || 8117);

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  forbidOnly: Boolean(process.env.CI),
  retries: 0,
  reporter: process.env.CI ? [["list"], ["github"]] : [["list"]],
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium", use: { browserName: "chromium" } }],
  webServer: {
    // The frontend is assumed built: `npm run build` is the step before this one,
    // locally and in CI.
    command: `npx vite preview --port ${port} --strictPort`,
    url: `http://127.0.0.1:${port}/`,
    reuseExistingServer: !process.env.CI,
    timeout: 60_000,
  },
});
