<script>
  /**
   * Plan types, each with the drivers that answer it.
   *
   * A **fixed list**, not a filter and not a picker. There are only a handful
   * of plan types by design -- they are a closed set in git -- so everything is
   * on one page, in order, with no selection to make before you can read
   * anything. A rail was tried and it was worse: a list of names beside a pane
   * reads like a set of filters, and the name looked like it belonged to the
   * status drawn next to it.
   *
   * The nesting is the model: a section is a *plan type* (the question), and
   * the rows inside it are *drivers* (one vendor's implementation of that
   * question). A driver is never a sibling of a plan type.
   *
   * The parameter table is the type's, and sits folded at the foot of its
   * section: it is reference material, and expanded it pushed the drivers off
   * the screen.
   *
   * Nothing sits to the right of a heading except the version. A status there
   * reads as an attribute of the name it shares a line with, so
   * "market_data_window  v1  driver" says the type is a driver. The rows below
   * already answer it: a driver row, or a row saying no driver is installed.
   *
   * A vendor row can also be a *request*: the driver queue asks an authoring
   * agent for code, and that job is the only state between "nobody wrote one"
   * and "one is installed". So the row says "driver being built" while it is in
   * flight and "driver build stopped" when a human is needed -- both used to
   * read "no driver installed", which drew "wait" and "never" identically.
   */
  import AppMark from "$lib/components/AppMark.svelte";
  import AppShell from "$lib/components/nb/AppShell.svelte";
  import DataTable from "$lib/components/nb/DataTable.svelte";
  import EmptyState from "$lib/components/nb/EmptyState.svelte";
  import { getView, postJson } from "$lib/api";
  import { when } from "$lib/format";

  let data = $state(null);
  let failure = $state("");
  // Retiring a stopped build: which build is in flight, and what the server
  // said if it refused. Keyed by id, because two vendors can both be stopped.
  let retiring = $state("");
  let retireError = $state({});

  // `#<type>` is an anchor into the list (the dashboard links to it);
  // `#<type>/<driver>` also opens that driver's row.
  let hash = $state(
    typeof window === "undefined" ? "" : window.location.hash.replace(/^#/, ""),
  );

  $effect(() => {
    if (typeof window === "undefined") return;
    const read = () => (hash = window.location.hash.replace(/^#/, ""));
    window.addEventListener("hashchange", read);
    return () => window.removeEventListener("hashchange", read);
  });

  $effect(() => {
    const controller = new AbortController();
    getView("/ui/plan-types.json", { signal: controller.signal })
      .then((body) => (data = body))
      .catch((error) => {
        if (error.name !== "AbortError") failure = error.message;
      });
    return () => controller.abort();
  });

  const fromHash = $derived(hash.includes("/") ? hash.split("/") : ["", ""]);
  const hashType = $derived(fromHash[0]);
  const hashDriver = $derived(fromHash[1] || "");

  // Which driver row is open, remembered per type so closing one stays closed.
  // `""` is an explicit "I closed it", which is a different fact from "no
  // choice yet" -- without that distinction the default row springs back open
  // the moment it is shut.
  let chosen = $state({});

  // Artefacts the runtime has but no plan type accepts. Not an error and not
  // hidden: "why is this driver doing nothing" is a question this page should
  // be able to answer.
  const orphans = $derived(
    (data?.drivers?.installed ?? []).filter((row) => row.answers.length === 0),
  );

  const columns = [
    { label: "parameter", width: "minmax(8rem, 0.9fr)" },
    { label: "type", width: "7.5rem" },
    { label: "constraints", width: "minmax(9rem, 1.1fr)" },
    { label: "what it means", width: "minmax(12rem, 2fr)" },
  ];

  // What a vendor row says when nothing is installed. The build is the difference
  // between "nobody wrote one" and "somebody is writing one": both used to read
  // "no driver installed", which made a queued vendor look like a dead end.
  function noDriverLabel(impl) {
    const build = impl.build;
    if (!build) return "no driver installed";
    if (build.open) return "driver being built";
    if (build.state === "needs_human") return "driver build stopped";
    return "no driver installed";
  }

  // The right-hand micro on a row with no driver: the one fact that row cannot
  // say in a pill. It is the build's *state*, not a sentence about it, because
  // the state is the vocabulary the queue uses.
  function buildNote(impl) {
    const build = impl.build;
    if (!impl.driver && build?.open) {
      const tries =
        build.attempts === 1 ? "attempt 1" : `attempt ${build.attempts}`;
      return tries;
    }
    if (!impl.driver && build?.state === "needs_human") {
      return build.attempts
        ? `${build.attempts} attempt${build.attempts === 1 ? "" : "s"}`
        : "stopped";
    }
    return "";
  }

  // When the driver that is installed was built. The artefact's own
  // `installed_at` is a date-only placeholder, so this comes from the job that
  // built it: `updated_at` on an `installed` row is the install itself. A row
  // with no build (an artefact put there by hand) has no answer, and says
  // nothing rather than "unknown".
  function builtWhen(impl) {
    const stamp = impl.build?.updated_at || impl.build?.created_at;
    return impl.driver && stamp ? when(stamp) : "";
  }

  // The micro on a row that has a driver: how much it claims to answer, and
  // when that code was built. Both, not one instead of the other -- the count
  // says what the driver covers, the date says whether it is the one that was
  // just built, which the pill's version number alone cannot answer while
  // scanning. One string rather than two template nodes, because Svelte trims
  // the whitespace between `{#if}` blocks and the row would otherwise read
  // "codes· built ...".
  function coverageNote(impl) {
    const count = impl.datasets.length;
    const codes = `${count} dataset ${count === 1 ? "code" : "codes"}`;
    const built = builtWhen(impl);
    return built ? `${codes} · built ${built}` : codes;
  }

  // Where the code for an installed driver is. Three deployment facts, kept in
  // the payload rather than typed here: the repository, its base branch (the
  // ref to fall back on when a build recorded no commit) and the artefact's
  // path inside it. No build, no commit, no link -- the page must not invent a
  // ref, and a hand-installed artefact genuinely has no commit to point at.
  function codeHref(impl) {
    const repo = data?.drivers?.repo ?? "";
    const path = impl.driver?.path ?? "";
    const ref = impl.build?.commit || data?.drivers?.base || "";
    return repo && path && ref
      ? `https://github.com/${repo}/tree/${ref}/${path}`
      : "";
  }

  // The authoring transcript, as a link only when the deployment named where a
  // *browser* opens its canvas: the store keeps the conversation *id*, so a
  // URL built from a guess would be a link to a 404. The payload carries the
  // canvas root, not the API address -- `http://openhands:8000` resolves
  // inside the app's network and nowhere else.
  function transcript(ref) {
    const canvas = (data?.drivers?.canvas_url ?? "").replace(/\/+$/, "");
    return canvas && ref ? `${canvas}/conversations/${ref}` : "";
  }

  function openDriverFor(type) {
    if (Object.hasOwn(chosen, type.name)) return chosen[type.name];
    if (hashType === type.name) return hashDriver;
    return type.implementations.find((impl) => impl.driver)?.driver?.name || "";
  }

  function toggle(type, impl, open) {
    chosen = { ...chosen, [type.name]: open ? "" : impl.driver.name };
    if (typeof window === "undefined") return;
    const fragment = open ? type.name : `${type.name}/${impl.driver.name}`;
    window.location.hash = fragment;
    hash = fragment;
  }

  /** Put a stopped build down, then re-read the page.
   *
   * `needs_human` is a stop, and a stop with no way out blocks its vendor for
   * ever: the loop refuses to queue another build while the newest one is
   * stopped. Retiring is how a person says "that attempt is finished with",
   * after which the next request for this vendor may ask for a new build. The
   * page re-reads rather than editing the row in place, because the answer is
   * the server's: the row after a retire is "no driver installed", which is a
   * different sentence from the one the page was just showing.
   */
  async function retireBuild(build) {
    retiring = build.id;
    retireError = { ...retireError, [build.id]: "" };
    try {
      await postJson(
        `/v1/driver-jobs/${encodeURIComponent(build.id)}/retire`,
        {},
      );
      data = await getView("/ui/plan-types.json");
    } catch (error) {
      retireError = { ...retireError, [build.id]: error.message };
    } finally {
      retiring = "";
    }
  }
</script>

<AppShell
  footer={false}
  title="data-download"
  nav={[
    { label: "Requests", href: "/", current: false },
    { label: "Datasets", href: "/datasets", current: false },
    { label: "Plan types", href: "/plan-types", current: true },
  ]}
>
  {#snippet mark()}
    <AppMark />
  {/snippet}

  <p class="lede">Plan types</p>

  {#if failure}
    <EmptyState cause="error" customMessage={failure} />
  {:else if !data}
    <p class="hint">Loading the registry…</p>
  {:else if data.types.length === 0}
    <EmptyState
      customMessage="No plan types are registered — every submission will be rejected."
    />
  {:else}
    {#each data.types as type (type.name)}
      <section class="plan-type" id={type.anchor}>
        <div class="type-head">
          <h2>{type.name}</h2>
          {#if type.vendor}
            <span class="pill">{type.vendor}</span>
          {/if}
          <span class="pill">v{type.version}</span>
        </div>
        <p class="type-desc">{type.description}</p>

        <h3 class="sub-head">drivers</h3>
        {#if type.implementations.length === 0}
          <p class="hint">
            No vendor is listed for this type, so no driver can answer it.
          </p>
        {:else}
          {#if !data.drivers.known}
            <p class="hint">
              Driver state is unknown — {data.drivers.detail ||
                "the driver runtime did not answer"} — so the rows below say what
              each vendor <em>should</em> have installed, not what does.
            </p>
          {/if}
          {@const openName = openDriverFor(type)}
          <ul class="drivers">
            {#each type.implementations as impl (impl.vendor)}
              {@const open = impl.driver && impl.driver.name === openName}
              <li class="driver-row" class:open>
                <button
                  type="button"
                  class="driver-line driver-toggle"
                  disabled={!impl.driver}
                  aria-expanded={open}
                  onclick={() => impl.driver && toggle(type, impl, open)}
                >
                  <span class="vendor">{impl.vendor}</span>
                  {#if impl.driver}
                    <span class="pill impl-on"
                      >{impl.driver.name} v{impl.driver.version}</span
                    >
                  {:else if impl.build?.open}
                    <span class="pill impl-on build-on"
                      >{noDriverLabel(impl)}</span
                    >
                  {:else}
                    <span
                      class="pill impl-off"
                      class:impl-bad={impl.build?.state === "needs_human"}
                      title={impl.build?.error ?? ""}
                      >{noDriverLabel(impl)}</span
                    >
                  {/if}
                  <span class="spacer"></span>
                  {#if buildNote(impl)}
                    <span class="micro" title={impl.build?.error ?? ""}
                      >{buildNote(impl)}</span
                    >
                  {:else}
                    <!-- Codes first, then when the code was written: see
                         `coverageNote`. -->
                    <span class="micro">{coverageNote(impl)}</span>
                  {/if}
                  {#if impl.driver}
                    <span class="chev" aria-hidden="true"
                      >{open ? "−" : "+"}</span
                    >
                  {/if}
                </button>
                {#if !impl.driver && impl.build?.state === "needs_human"}
                  <!-- A sibling of the toggle, not a child: the row above is one
                       button and buttons do not nest. Only a stop offers this,
                       because only a stop is a dead end -- a queued build is
                       already being worked and an installed driver has nothing
                       to put down. -->
                  <div class="retire">
                    <p class="retire-why">
                      The build stopped, and its vendor stays blocked: the loop
                      will not queue another while this one is down.
                    </p>
                    <button
                      class="btn verb"
                      type="button"
                      data-verb="retire"
                      data-build={impl.build.id}
                      disabled={retiring !== "" || !impl.build.id}
                      onclick={() => retireBuild(impl.build)}
                    >
                      {retiring === impl.build.id
                        ? "Retiring…"
                        : "Retire this build"}
                    </button>
                    {#if retireError[impl.build.id]}
                      <!-- The server's words, unedited: a refusal paraphrased
                           is a guess. -->
                      <p class="retire-failure">
                        {retireError[impl.build.id]}
                      </p>
                    {/if}
                  </div>
                {/if}
                {#if open}
                  <!-- The match as a rule rather than as prose: this vendor,
                       and one of these dataset codes. Nothing else: the name
                       and version are already the row's pill, and protocol can
                       only be 1 here (a mismatch refuses discovery outright). -->
                  <dl class="driver-detail">
                    <dt>matches</dt>
                    <dd>
                      vendor <code>{impl.vendor}</code>
                      {#if impl.datasets.length}
                        · dataset is one of
                        {#each impl.datasets as code (code)}
                          <code class="code-chip">{code}</code>
                        {/each}
                      {:else}
                        · any dataset
                      {/if}
                    </dd>
                    <!-- Where the code is. A version number is not a
                         destination: `databento v2` in the pill says which
                         artefact is running and nothing about where to read
                         it, so the row links to the directory in the driver
                         repo, pinned to the commit that was installed. -->
                    <dt>code</dt>
                    <dd>
                      {#if codeHref(impl)}
                        <a
                          class="ref"
                          href={codeHref(impl)}
                          target="_blank"
                          rel="noreferrer">{impl.driver.path}/</a
                        >
                        {#if impl.build?.commit}
                          at <code>{impl.build.commit.slice(0, 7)}</code>
                        {:else}
                          on <code>{data.drivers.base || "main"}</code>
                        {/if}
                      {:else}
                        <span class="muted"
                          >no repository recorded for this artefact</span
                        >
                      {/if}
                    </dd>
                    <!-- The authoring request behind this row. It is the one
                         thing on the page that has an *origin*: a driver is not
                         a fact of nature, it was written, and when, by whom and
                         on what branch are what say so. Shown only when there
                         is one, because a hand-installed artefact has no such
                         story. -->
                    {#if impl.build}
                      <!-- When is the row's own micro, not repeated here: the
                           date answers "is this the one that was just built?",
                           which is asked while scanning, and saying it twice
                           in one card is noise. This line is the *what*: what
                           state the build reached, how many attempts, on which
                           branch, and where to read it. -->
                      <dt>build</dt>
                      <dd>
                        {impl.build.state}
                        {#if impl.build.attempts}
                          · attempt{impl.build.attempts === 1 ? "" : "s"}
                          {impl.build.attempts}
                        {/if}
                        {#if impl.build.branch}
                          · <code>{impl.build.branch}</code>
                        {/if}
                        {#if impl.build.commit}
                          @ <code>{impl.build.commit.slice(0, 7)}</code>
                        {/if}
                        <!-- Where the work was actually done, when the authoring
                             agent left a transcript. Linked only if this
                             deployment says where its canvas is served; the id
                             alone still names the conversation. -->
                        {#if impl.build.author_ref}
                          ·
                          {#if transcript(impl.build.author_ref)}
                            <a
                              class="ref"
                              href={transcript(impl.build.author_ref)}
                              target="_blank"
                              rel="noreferrer">transcript</a
                            >
                          {:else}
                            <span class="ref" title={impl.build.author_ref}
                              >authored in openhands</span
                            >
                          {/if}
                        {/if}
                      </dd>
                    {/if}
                  </dl>
                {/if}
              </li>
            {/each}
          </ul>
        {/if}

        <details class="asks">
          <summary>
            what the type asks · {type.schema.required_count} required, {type
              .schema.optional_count} optional
          </summary>
          {#if type.schema.fields.length}
            <DataTable {columns} items={type.schema.fields}>
              {#snippet row(field)}
                <span class="name"
                  ><code>{field.name}</code>{#if field.required}<span
                      class="req">required</span
                    >{/if}</span
                >
                <span class="type"><code>{field.type}</code></span>
                <span class="cons">
                  {#if field.constraints.length}
                    {#each field.constraints as constraint, index (constraint)}
                      {constraint}{#if index < field.constraints.length - 1}<span
                          class="quiet"
                        >
                          ·
                        </span>{/if}
                    {/each}
                  {:else}
                    <span class="quiet">—</span>
                  {/if}
                </span>
                <span class="desc">{field.description || "—"}</span>
              {/snippet}
            </DataTable>
          {/if}
          <details class="raw">
            <summary>raw JSON schema</summary>
            <pre>{type.schema.raw}</pre>
          </details>
        </details>
      </section>
    {/each}

    {#if orphans.length}
      <!-- A section rather than a place in the list above, because these are
           artefacts, not questions: nothing here can be filed as them. -->
      <section class="plan-type orphans" id="unmatched">
        <div class="type-head">
          <h2>drivers with no plan type</h2>
        </div>
        <p class="type-desc">
          Installed in the runtime, but no plan type above lists their vendor,
          so nothing can be filed as them.
        </p>
        <ul class="drivers">
          {#each orphans as row (row.name + row.version)}
            <li class="driver-row">
              <div class="driver-line">
                <code class="driver-name">{row.name}</code>
                <span class="pill">v{row.version}</span>
                <span class="vendor">{row.vendor || "—"}</span>
              </div>
            </li>
          {/each}
        </ul>
      </section>
    {/if}
  {/if}
</AppShell>

<style>
  /* One list, in order. A type is a section, not a card: cards read as peers
     that happen to be side by side, and the point is that this is the whole
     fixed set, top to bottom. */
  .plan-type {
    padding: 0 0 18px;
    margin: 0 0 18px;
    border-bottom: 1px solid var(--line);
    /* The dashboard links straight to /plan-types#<type>, and the bar is
       sticky, so without this the heading lands underneath it. */
    scroll-margin-top: 72px;
  }

  .plan-type:last-child,
  .orphans {
    border-bottom: 0;
    padding-bottom: 0;
    margin-bottom: 0;
  }

  .type-head {
    display: flex;
    align-items: baseline;
    gap: 8px;
    flex-wrap: wrap;
    margin-bottom: 4px;
  }

  .type-head h2 {
    margin: 0;
    font-size: 15px;
    font-weight: 600;
    letter-spacing: -0.005em;
    color: var(--ink);
  }

  .type-head .spacer,
  .driver-line .spacer {
    flex: 1 1 auto;
  }

  .type-desc {
    margin: 0 0 10px;
    color: var(--ink-dim);
    max-width: 78ch;
  }

  /* "drivers" labels the list inside a section; it is not part of the page's
     outline, which is the plan types. */
  .sub-head {
    margin: 12px 0 6px;
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--ink-muted);
  }

  .drivers {
    list-style: none;
    margin: 0 0 12px;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  /* One row per driver. The row is the button that opens it, so the hit target
     is the whole line rather than a word in it. */
  .driver-row {
    border: 1px solid var(--line);
    border-radius: var(--radius-sm);
    background: var(--bg-sunken);
    overflow: hidden;
  }

  .driver-row.open {
    border-color: var(--accent);
  }

  /* A stop's way out: under the row, in the same box, because it is a fact
     about this row and not a verb on the page. Quiet copy, one button -- the
     person has already read the error in the pill's title. */
  .retire {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
    padding: 0 10px 9px;
  }

  .retire-why {
    margin: 0;
    flex: 1 1 16rem;
    font-size: 12px;
    line-height: 1.45;
    color: var(--ink-dim);
  }

  .retire-failure {
    margin: 0;
    flex: 1 1 100%;
    font-size: 12px;
    color: var(--bad);
  }

  .driver-line {
    display: flex;
    align-items: baseline;
    gap: 8px;
    flex-wrap: wrap;
    width: 100%;
    padding: 8px 10px;
    font: inherit;
    font-size: 12px;
    color: var(--ink);
    text-align: left;
    background: transparent;
    border: 0;
  }

  .driver-toggle {
    cursor: pointer;
  }

  .driver-toggle:disabled {
    cursor: default;
    color: var(--ink-dim);
  }

  .driver-name,
  .vendor {
    font-weight: 600;
    color: var(--ink);
  }

  .chev {
    color: var(--ink-muted);
    font-variant-numeric: tabular-nums;
  }

  .impl-on {
    color: var(--accent);
    background: var(--accent-soft);
  }

  /* A transcript link sits inside a definition line, so it must not add colour
     noise: accent, no underline until hovered, and the same size as the code
     beside it. */
  .ref {
    color: var(--accent);
    text-decoration: none;
  }

  .ref:hover {
    text-decoration: underline;
  }

  /* Not installed is not an error: the type is still readable, and a request
     filed as it would wait for the driver rather than fail. */
  .impl-off {
    color: var(--ink-muted);
    background: transparent;
    border: 1px dashed var(--line);
  }

  /* A build that stopped is not the same as a build that has not started, and
     the pill is where that difference has to be visible: a dashed pill reads as
     "nothing here yet", which would send a reader away from the one row that
     needs a human. */
  .impl-off.impl-bad {
    color: var(--bad);
    border-style: solid;
    border-color: var(--bad);
  }

  /* In flight, and in the accent colour: the row is doing something, and the
     pill it replaces (a dashed "nothing") said the opposite. */
  .build-on {
    color: var(--accent);
    background: var(--accent-soft);
  }

  /* The open driver: a small definition list, because each line answers one
     question and the answers do not need prose around them. */
  .driver-detail {
    display: grid;
    grid-template-columns: 4.5rem minmax(0, 1fr);
    gap: 4px 10px;
    margin: 0;
    padding: 0 10px 10px;
    font-size: 12px;
    color: var(--ink-dim);
  }

  .driver-detail dt {
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--ink-muted);
    padding-top: 2px;
  }

  .driver-detail dd {
    margin: 0;
    overflow-wrap: anywhere;
  }

  .code-chip {
    display: inline-block;
    margin: 0 2px;
    padding: 0 5px;
    border: 1px solid var(--line);
    border-radius: var(--radius-sm);
    font-size: 11px;
  }

  /* The field table is reference, not the answer: folded, so what a type asks
     never pushes the next type off the screen. */
  .asks {
    margin-top: 10px;
  }

  .asks > summary,
  .raw > summary {
    cursor: pointer;
    color: var(--ink-muted);
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.04em;
    text-transform: uppercase;
  }

  .asks > summary:hover,
  .raw > summary:hover {
    color: var(--ink);
  }

  .asks[open] > summary {
    margin-bottom: 8px;
  }

  .raw {
    margin-top: 10px;
  }

  .req {
    display: inline-block;
    margin-left: 6px;
    padding: 0 5px;
    border-radius: var(--radius-pill);
    font-size: 9px;
    font-weight: 600;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    color: var(--accent);
    background: var(--accent-soft);
  }

  pre {
    margin: 8px 0 0;
    padding: 10px;
    overflow: auto;
    background: var(--bg-sunken);
    border: 1px solid var(--line);
    border-radius: var(--radius-sm);
    font-size: 11px;
  }

  @media (max-width: 720px) {
    .driver-detail {
      grid-template-columns: minmax(0, 1fr);
      gap: 2px;
    }

    .driver-detail dt {
      padding-top: 6px;
    }
  }
</style>
