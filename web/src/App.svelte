<script>
  /**
   * Three pages, one bundle. FastAPI serves this same index.html for `/`,
   * `/plan-types` and `/requests/<id>` (all views of live state, all behind the
   * read scope), so the router is `location.pathname` and nothing else. No hash
   * routing: the dashboard's `#plan_type` anchors and the plan-type page's TOC
   * are real fragment links and have to keep working.
   */
  import Dashboard from "$lib/views/Dashboard.svelte";
  import PlanTypes from "$lib/views/PlanTypes.svelte";
  import RequestDetail from "$lib/views/RequestDetail.svelte";

  const path =
    typeof window === "undefined"
      ? "/"
      : window.location.pathname.replace(/\/+$/, "") || "/";

  const requestMatch = path.match(/^\/requests\/([^/]+)$/);
  const view = requestMatch
    ? "request"
    : path === "/plan-types"
      ? "plans"
      : "requests";
</script>

{#if view === "request"}
  <RequestDetail id={decodeURIComponent(requestMatch[1])} />
{:else if view === "plans"}
  <PlanTypes />
{:else}
  <Dashboard />
{/if}
