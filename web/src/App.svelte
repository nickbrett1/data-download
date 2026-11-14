<script>
  /**
   * Two pages, one bundle. FastAPI serves this same index.html for `/` and
   * `/plan-types` (they are both views of live state, both behind the read
   * scope), so the router is `location.pathname` and nothing else. No hash
   * routing: the dashboard's `#plan_type` anchors and the plan-type page's TOC
   * are real fragment links and have to keep working.
   */
  import Dashboard from "$lib/views/Dashboard.svelte";
  import PlanTypes from "$lib/views/PlanTypes.svelte";

  const path =
    typeof window === "undefined"
      ? "/"
      : window.location.pathname.replace(/\/+$/, "") || "/";
  const view = path === "/plan-types" ? "plans" : "requests";
</script>

{#if view === "plans"}
  <PlanTypes />
{:else}
  <Dashboard />
{/if}
