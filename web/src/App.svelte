<script>
  /**
   * Three pages, one bundle. FastAPI serves this same index.html for `/`,
   * `/plan-types` and `/requests/<id>`, so the router is `location.pathname`
   * and nothing else. No hash routing: the dashboard's `#plan_type` anchors and
   * the plan-type page's TOC are real fragment links and have to keep working.
   *
   * The lock lives here rather than in each view, because it is one fact about
   * the browser and every view needs the same answer. A view that gets 401
   * dispatches `auth:needed`; this replaces it with the gate and remounts on
   * unlock, so no view has to know what a token is.
   */
  import AppMark from "$lib/components/AppMark.svelte";
  import AppShell from "$lib/components/nb/AppShell.svelte";
  import Gate from "$lib/components/Gate.svelte";
  import Dashboard from "$lib/views/Dashboard.svelte";
  import PlanTypes from "$lib/views/PlanTypes.svelte";
  import RequestDetail from "$lib/views/RequestDetail.svelte";
  import { rememberToken, token } from "$lib/api";

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

  // `token()` also consumes a `#token=` fragment: the address bar is clean by
  // the time anything renders.
  let bearer = $state(token());
  let refused = $state(false);
  let revision = $state(0);

  function unlock(value) {
    const kept = rememberToken(value);
    if (!kept) return;
    bearer = kept;
    refused = false;
    revision += 1;
  }

  $effect(() => {
    if (typeof window === "undefined") return;
    const needed = () => {
      bearer = "";
      refused = true;
    };
    window.addEventListener("auth:needed", needed);
    return () => window.removeEventListener("auth:needed", needed);
  });
</script>

{#if !bearer}
  <AppShell footer={false} title="data-download">
    {#snippet mark()}
      <AppMark />
    {/snippet}
    <Gate {refused} onunlock={unlock} />
  </AppShell>
{:else}
  {#key revision}
    {#if view === "request"}
      <RequestDetail id={decodeURIComponent(requestMatch[1])} />
    {:else if view === "plans"}
      <PlanTypes />
    {:else}
      <Dashboard />
    {/if}
  {/key}
{/if}
