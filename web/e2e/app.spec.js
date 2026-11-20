import { expect, test } from "@playwright/test";

// Fixed clock rendering: the `filed` column is local time, and a test that
// asserted a string would otherwise pass in London and fail in Auckland.
test.use({ timezoneId: "UTC" });
import {
  blockedBook,
  dashboard,
  datasets,
  draftDetail,
  emptyBook,
  planTypes,
  requestDetail,
} from "./fixtures.js";

// Resource-type routing, not a URL glob: `**/stats` would also match the
// document navigation and hand the browser JSON instead of the app.
// What the server says the tailnet hop asserted. Overridable per test: the
// approve card's whole behaviour is a function of this answer.
const SERVED = { login: "nick@example.com", approver: true };

async function mock(
  page,
  { requests, types, detail, tree, who, whoStatus = 200 } = {},
) {
  await page.route(
    "**/v1/whoami",
    (route) =>
      route.fulfill({
        status: whoStatus,
        contentType: "application/json",
        body: JSON.stringify(
          whoStatus === 200
            ? (who ?? SERVED)
            : {
                error: {
                  code: "not_attested",
                  message:
                    "no Tailscale-User-Login header: this request arrived " +
                    "from 192.168.1.40 directly, not through `tailscale serve`.",
                },
              },
        ),
      }),
    { times: 99 },
  );
  await page.route(
    "**/ui/**",
    (route) => {
      const url = route.request().url();
      const body = url.includes("plan-types")
        ? (types ?? planTypes)
        : url.includes("/ui/datasets")
          ? (tree ?? datasets)
          : url.includes("/ui/request/")
            ? (detail ?? requestDetail)
            : (requests ?? dashboard);
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(body),
      });
    },
    { times: 99 },
  );
}

test.describe("the request book", () => {
  test("is one list with a badge per row, and the figures line up", async ({
    page,
  }) => {
    await mock(page);
    await page.goto("/");

    await expect(page.locator(".tbl-row")).toHaveCount(5);
    // Four columns: what it is, where it is, what it costs, when it was asked
    // for. "why it matters" was the one removed -- on a finished row it either
    // repeated the badge or rendered a dash, and its useful half (what a
    // blocked row is short of) is a qualification of the state, so it sits
    // under the badge now.
    await expect(page.locator(".tbl-head .col-label")).toHaveText([
      "request",
      "state",
      "quote",
      "filed",
    ]);

    // The columns of a grid share one template, which is what makes the
    // numeric column line up rather than land wherever the previous cell ended.
    const template = await page
      .locator(".tbl-row")
      .first()
      .evaluate((el) => getComputedStyle(el).gridTemplateColumns);
    expect(template.split(" ").length).toBe(4);

    const first = page.locator(".tbl-row").first();
    // Phase 4: the quote is on the row, at the precision a purchase needs.
    await expect(first.locator(".quote")).toHaveText("$0.0412");
    // When it was filed, in a form a person reads. Not "1h 4m": an age answers
    // a question nobody asked and has to be converted back into a clock.
    await expect(first.locator(".filed")).toHaveText("14 Nov 23:10");
    await expect(first.locator(".pill")).toHaveText("Succeeded");
  });

  test("a stuck condition outranks the state it is stuck in", async ({
    page,
  }) => {
    await mock(page);
    await page.goto("/");

    // The second row filed: `running`, and stuck.
    const running = page.locator(".tbl-row").nth(1);
    // The badge says `running`; the line under it is the fact that makes the
    // row wrong. One pill, not two: a running row is not in a second state, it
    // is in the one state with a problem.
    await expect(running.locator(".pill")).toHaveCount(1);
    await expect(running.locator(".pill")).toHaveText("Running");
    await expect(running.locator(".state .detail")).toHaveText("lease_expired");
    // And it is coloured as a fault, which is the whole reason the detail line
    // carries a tone -- otherwise a stuck row looks like an ordinary one.
    await expect(running.locator(".state .detail")).toHaveClass(/\bbad\b/);
  });

  test("the facet is one choice from N, drawn as a histogram", async ({
    page,
  }) => {
    await mock(page);
    await page.goto("/");

    const group = page.locator(".facet-group").first();
    const radios = group.locator('input[type="radio"]');
    // All, plus the seven facets. `waiting` used to be one of them; it is now
    // three (needs params / no plan / requested), because "waiting on a plan"
    // named a state and hid an owner.
    await expect(radios).toHaveCount(8);
    // One name for the whole group, or the browser lets you pick two.
    expect(
      new Set(await radios.evaluateAll((all) => all.map((i) => i.name))).size,
    ).toBe(1);

    // The bar is the count, relative to the largest option in its own group:
    // 2 finished out of 2 is a full bar.
    const widthOf = (label) =>
      group
        .locator(".facet-option")
        .filter({ hasText: label })
        .locator(".option-bar")
        .evaluate((bar) => parseFloat(getComputedStyle(bar).width));
    const [all, done] = [await widthOf("All"), await widthOf("Finished")];
    // Two finished of five requests: the bar is the count, relative to the
    // largest option in its own group.
    expect(done / all).toBeCloseTo(2 / 5, 1);
  });

  test("choosing a facet narrows the list and lands in the URL", async ({
    page,
  }) => {
    await mock(page);
    await page.goto("/");

    await page.locator(".facet-option").filter({ hasText: "Approval" }).click();
    await expect(page).toHaveURL(/show=todo/);
    await expect(page.locator(".tbl-row")).toHaveCount(1);
    await expect(page.locator(".tbl-row")).toContainText("#3");

    // A filter that matches nothing says so, and offers the way back.
    await page.locator(".facet-option").filter({ hasText: "Running" }).click();
    await expect(page.locator(".tbl-row")).toHaveCount(0);
    await expect(page.locator(".empty-state")).toContainText(
      "No matches for active filters",
    );

    // Clicking the active option clears it -- a single-select facet with no way
    // back is a trap.
    await page.locator(".facet-option").filter({ hasText: "Running" }).click();
    await expect(page).not.toHaveURL(/show=/);
    await expect(page.locator(".tbl-row")).toHaveCount(5);
  });

  test("an approved row says it is approved, and that nothing has it", async ({
    page,
  }) => {
    // Two words for two different questions, and they must not look like rival
    // answers to one. The badge answers "where is this"; the line under it
    // answers "what is missing". It used to say the participle `unclaimed`,
    // which beside a badge reading `Approved` read as a contradiction -- the
    // reader's question was "so which is it?".
    await mock(page);
    await page.goto("/");

    const row = page.locator(".tbl-row").filter({ hasText: "#3" });
    await expect(row.locator(".pill")).toHaveText("Approved");
    await expect(row.locator(".state .detail")).toHaveText(
      "no worker has it yet",
    );
  });

  test("an unapproved row says what approving it would cost", async ({
    page,
  }) => {
    // This row used to say it twice: the pill read `draft` and the next column
    // read `needs approval`. One fact, two spellings -- and `draft` was the
    // wrong one, since a draft is unsubmitted and editable and this row is
    // neither. The badge now names what is being waited on; the line under it
    // carries the fact the badge cannot: what approving would authorise.
    const draft = {
      ...dashboard.rows[2],
      // A keyed `each` is what makes the table correct and what makes a copied
      // id a crash: the row needs its own.
      id: "req_draft06",
      seq: 6,
      state: "draft",
      max_usd: 5,
      approved_unclaimed: false,
    };
    await mock(page, {
      requests: {
        ...dashboard,
        rows: [...dashboard.rows, draft],
        total: dashboard.total + 1,
      },
    });
    await page.goto("/");

    const row = page.locator(".tbl-row").filter({ hasText: "#6" });
    await expect(row.locator(".pill")).toHaveCount(1);
    await expect(row.locator(".pill")).toHaveText("Awaiting approval");
    await expect(row.locator(".state .detail")).toHaveText("up to $5");
    await expect(page.locator("body")).not.toContainText("needs approval");
  });

  test("a filter nobody knows about falls back to everything", async ({
    page,
  }) => {
    await mock(page);
    await page.goto("/?show=nonsense");
    await expect(page.locator(".tbl-row")).toHaveCount(5);
    // and it is corrected in the URL, so a refresh does not re-break it
    await expect(page).not.toHaveURL(/show=/);
  });

  test("?show= from a link filters on load", async ({ page }) => {
    await mock(page);
    await page.goto("/?show=broken");
    await expect(page.locator(".tbl-row")).toHaveCount(1);
    await expect(page.locator(".tbl-row")).toContainText("#2");
  });

  test("a link between the two pages carries no credential", async ({
    page,
  }) => {
    await mock(page);
    await page.goto("/");
    // Both views are public, so a nav tab is just a tab. No token in an href
    // anywhere on the page: a credential in a URL is a credential in a log.
    const links = page.locator('a[href="/plan-types"]');
    expect(await links.count()).toBeGreaterThan(0);
    const hrefs = await page
      .locator("a[href]")
      .evaluateAll((els) => els.map((el) => el.getAttribute("href")));
    expect(hrefs.filter((href) => href.includes("token="))).toEqual([]);
    await expect(page).not.toHaveURL(/token=/);
  });

  test("the head is the home-screen entry, not a bare tab", async ({
    page,
  }) => {
    await mock(page);
    await page.goto("/");
    // These are the pre-existing phone bookmarks: the URLs are baked into home
    // screens, so the markup that names them cannot drift.
    await expect(page.locator('link[rel="apple-touch-icon"]')).toHaveAttribute(
      "href",
      "/static/apple-touch-icon.png",
    );
    await expect(page.locator('link[rel="manifest"]')).toHaveAttribute(
      "href",
      "/site.webmanifest",
    );
    await expect(page.locator('meta[name="theme-color"]')).toHaveAttribute(
      "content",
      "#0f172a",
    );
    await expect(
      page.locator('meta[name="apple-mobile-web-app-capable"]'),
    ).toHaveAttribute("content", "yes");
    // The bar carries the product name and the tabs, and nothing else: the
    // subtitle said "the request book" two lines above a heading that said the
    // same thing. Where you are is the highlighted tab and the page title.
    await expect(page.locator(".topbar")).toContainText("data-download");
    await expect(page.locator(".topbar .titles .micro")).toHaveCount(0);
  });

  test("the app mark sits at the left of the bar, before the name", async ({
    page,
  }) => {
    await mock(page);
    await page.goto("/");

    const mark = page.locator(".topbar .mark svg");
    await expect(mark).toBeVisible();
    await expect(mark).toHaveAttribute("aria-hidden", "true");

    // Before the wordmark, and on the same optical line: a 22px mark with the
    // 13px name to its right, not a badge stranded on top of the bar.
    const boxes = await page.evaluate(() => {
      const mark = document
        .querySelector(".topbar .mark")
        .getBoundingClientRect();
      const name = document
        .querySelector(".topbar .wordmark")
        .getBoundingClientRect();
      const svg = getComputedStyle(document.querySelector(".topbar .mark svg"));
      return {
        markLeft: mark.left,
        markHeight: mark.height,
        nameLeft: name.left,
        markMid: mark.top + mark.height / 2,
        nameMid: name.top + name.height / 2,
        colour: svg.color,
      };
    });
    expect(boxes.markLeft).toBeLessThan(boxes.nameLeft);
    expect(boxes.markHeight).toBeCloseTo(22, 0);
    expect(Math.abs(boxes.markMid - boxes.nameMid)).toBeLessThan(4);
    // The accent, not a baked hex: it follows the theme.
    expect(boxes.colour).toBe("rgb(125, 211, 252)");
  });

  test("the intro says what the book is, not what it cannot do", async ({
    page,
  }) => {
    // Two sentences were removed from above the list. "Phase 1: nothing
    // advances past draft or blocked. Waiting on a plan is not breakage."
    // explained the badge vocabulary, which the badge already speaks on the
    // row itself. "Nothing here can approve or change a request" described an
    // absence: the proof of it is that there is no button, and a reader who
    // came to look at the book was being told what the book is not.
    await mock(page);
    await page.goto("/");
    await expect(page.locator(".tbl-row")).toHaveCount(5);
    const body = await page.locator("body").innerText();
    expect(body).not.toContain("Phase 1");
    expect(body).not.toContain("not breakage");
    expect(body).not.toContain("approval is a CLI");
    expect(body).not.toContain("cannot reach");

    // The paragraph itself is gone too: three sentences of intro above six
    // rows was most of the page. The title says what the page is, the badge
    // says why a row matters, and the filter strip says what is in the book.
    await expect(page.locator(".sub-lede")).toHaveCount(0);
  });

  test("an empty book says it once", async ({ page }) => {
    // It used to say it three times: the block's "zero results" label, "No
    // requests yet.", and the block's stock hint - under a filter strip of six
    // zeroes. One sentence, and the only one the stock hint could not say:
    // nothing on this page puts a request in the book.
    await mock(page, { requests: emptyBook });
    await page.goto("/");

    // The door is the API, not a CLI: this service ships no CLI binary, and the
    // first version of this sentence said "filed from the CLI", which was an
    // invention. `POST /v1/requests` is what actually creates a row.
    await expect(page.locator(".empty-book")).toHaveText(
      /^No requests yet — requests arrive through the API \(POST \/v1\/requests\), not from this page\.$/,
    );
    const body = await page.locator("body").innerText();
    expect(body.toLowerCase()).not.toContain("zero results");
    expect(body).not.toContain("No items have been added");
    expect(body.match(/No requests yet/g)).toHaveLength(1);

    // Nothing to narrow, so no strip of zeroes above the sentence.
    await expect(page.locator(".filter-panel")).toHaveCount(0);
  });

  test("no console errors", async ({ page }) => {
    const errors = [];
    page.on(
      "console",
      (msg) => msg.type() === "error" && errors.push(msg.text()),
    );
    await mock(page);
    await page.goto("/");
    await expect(page.locator(".tbl-row")).toHaveCount(5);
    expect(errors).toEqual([]);
  });

  test("on a phone the header row goes and each record wraps", async ({
    page,
  }) => {
    await mock(page);
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto("/");
    await expect(page.locator(".tbl-head")).toBeHidden();
    // Six columns on a phone is a scrollbar or a truncated id; the record wraps
    // instead, and stays a record.
    const display = await page
      .locator(".tbl-row")
      .first()
      .evaluate((el) => getComputedStyle(el).flexWrap);
    expect(display).toBe("wrap");
  });
});

test.describe("plan types", () => {
  test("every type is a card with a table of its parameters", async ({
    page,
  }) => {
    await mock(page);
    await page.goto("/plan-types");

    await expect(page.locator(".type-card")).toHaveCount(2);
    await expect(page.locator(".toc a")).toHaveText([
      "databento_batch_download",
      "http_fetch",
    ]);

    const first = page.locator(".type-card").first();
    await expect(first).toContainText("2 required · 1 optional");
    await expect(first.locator(".tbl-row")).toHaveCount(3);
    await expect(first.locator(".tbl-head .col-label")).toHaveText([
      "parameter",
      "type",
      "constraints",
      "what it means",
    ]);
    // The enum is spelled out, not hidden behind a schema reference: that is the
    // question the page exists to answer.
    await expect(first).toContainText("enum: GLBX.MDP3, XNAS.ITCH");
    // A field with no description is an em dash, not an empty cell.
    await expect(first.locator(".tbl-row").nth(2).locator(".desc")).toHaveText(
      "—",
    );
  });

  // The Svelte port kept the header count and dropped the per-field marker, so
  // the page said *how many* parameters were required but not *which*: a reader
  // with three fields had to open the raw JSON to find out. The count is not a
  // substitute for the tag, so both are asserted here.
  async function expectRequiredMarkers(page, card) {
    await expect(card.locator(".type-head .micro")).toHaveText(
      "2 required · 1 optional",
    );

    const required = card.locator(".tbl-row").filter({ hasText: "dataset" });
    // Visible, not merely present: the marker sits inline after the name
    // because DataTable drops the header row under 600px, and a hidden tag
    // would be the same regression in a quieter form.
    await expect(required.locator(".req")).toBeVisible();
    await expect(required.locator(".req")).toHaveText("required");

    const optional = card.locator(".tbl-row").filter({ hasText: "symbols" });
    await expect(optional.locator(".req")).toHaveCount(0);
  }

  test("each parameter says whether it is required", async ({ page }) => {
    await mock(page);
    await page.goto("/plan-types");

    await expectRequiredMarkers(page, page.locator(".type-card").first());
    // Property order stays the YAML's fill-in order: required first is a
    // coincidence here (`dataset`, `start`), not a rule the page imposes.
    await expect(
      page.locator(".type-card").first().locator(".tbl-row .name code"),
    ).toHaveText(["dataset", "start", "symbols"]);
  });

  test("the marker survives a phone, where the header row does not", async ({
    page,
  }) => {
    await mock(page);
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto("/plan-types");

    const first = page.locator(".type-card").first();
    // The reason the marker is inline and not a fifth column.
    await expect(first.locator(".tbl-head")).toBeHidden();
    await expectRequiredMarkers(page, first);

    // DataTable truncates every cell (`.tbl-row :global(*)`), and the tag sits
    // inside `.name`: an overflowing cell would clip it while `toBeVisible()`
    // still passed, since clipping does not zero a bounding box.
    const box = await first
      .locator(".tbl-row")
      .first()
      .evaluate((row) => {
        const name = row.querySelector(".name").getBoundingClientRect();
        const tag = row.querySelector(".req").getBoundingClientRect();
        return { nameRight: name.right, tagRight: tag.right };
      });
    expect(box.tagRight).toBeLessThanOrEqual(box.nameRight + 1);
  });

  test("the card is the table and the raw schema, and no glossary under it", async ({
    page,
  }) => {
    // A definition list used to sit under each table: extra fields, verify,
    // resume, source. Four labels of prose about a schema, on a page whose job
    // is to answer "which fields does this plan take?". It went, and the card
    // is now the description, the parameter rows, and the collapsed schema -
    // the last of which is the document itself, so nothing is lost.
    await mock(page);
    await page.goto("/plan-types");

    await expect(page.locator(".kv")).toHaveCount(0);
    const body = await page.locator("body").innerText();
    for (const gone of [
      "extra fields",
      "verify",
      "resume",
      "Rejected",
      "Accepted",
    ]) {
      expect(body.toLowerCase()).not.toContain(gone.toLowerCase());
    }

    const first = page.locator(".type-card").first();
    await expect(first.locator(".tbl-row")).toHaveCount(3);
    // The raw schema still declares its dialect, so a copy-paste has the rules.
    await expect(first.locator("details pre")).toContainText("draft/2020-12");
  });

  test("nothing on the page can change the registry", async ({ page }) => {
    await mock(page);
    await page.goto("/plan-types");
    // The registry is git, not the database: a control here would be a lie.
    await expect(page.locator("form")).toHaveCount(0);
    await expect(page.locator("input")).toHaveCount(0);
    // The other page is one click away, and the token rides along or it 401s.
    await expect(page.locator(".main-nav a").first()).toHaveText("Requests");
  });

  test("an empty registry says every submission will be rejected", async ({
    page,
  }) => {
    await mock(page, {
      types: { types: [] },
    });
    await page.goto("/plan-types");
    await expect(page.locator(".empty-state")).toContainText(
      "every submission will be rejected",
    );
  });
});

// The regression that made the Svelte port worse than the Jinja pages on a
// phone: navigation was a state pill (a destination that looked like a badge),
// and the page intro had no type because `.lede` was never defined. Both are
// measurable, so they are measured here at 390px rather than eyeballed.
test.describe("the shell on a phone", () => {
  test.use({ viewport: { width: 390, height: 780 } });

  async function expectTabs(page, current) {
    const tabs = page.locator(".main-nav a");
    await expect(tabs).toHaveCount(3);
    await expect(tabs).toHaveText(["Requests", "Datasets", "Plan types"]);
    // Exactly one tab is the current page, and it is the right one.
    await expect(page.locator('.main-nav a[aria-current="page"]')).toHaveCount(
      1,
    );
    await expect(page.locator('.main-nav a[aria-current="page"]')).toHaveText(
      current,
    );
    // Both ends of the row are reachable. app-shell 1.16.1 gives a squeezed
    // tab row `overflow-x: auto` with no scrollbar; with three labels at 390px
    // the row is ~24px wider than the bar, so the *last* tab is clipped until
    // the row is scrolled. That is the block's own contract ("momentum
    // scrolling still reaches the rest"), and it is the thing worth asserting:
    // a destination nobody can scroll to is the bug, not a tab 24px wide of
    // the edge. A block edited here to wrap the row fails `check-nb-blocks`,
    // rightly -- the wrap belongs in an app-shell release (upstream), and
    // then this test gets its `scrollWidth <= clientWidth` back.
    const edges = await page.locator(".main-nav").evaluate((el) => {
      const box = el.getBoundingClientRect();
      const within = (node) => {
        const r = node.getBoundingClientRect();
        return r.left >= box.left - 1 && r.right <= box.right + 1;
      };
      const tabs = [...el.querySelectorAll("a")];
      el.scrollLeft = 0;
      const first = within(tabs[0]);
      el.scrollLeft = el.scrollWidth;
      const last = within(tabs[tabs.length - 1]);
      el.scrollLeft = 0;
      return { first, last };
    });
    expect(edges.first).toBe(true);
    expect(edges.last).toBe(true);
  }

  async function expectLede(page, text) {
    const lede = page.locator(".lede");
    await expect(lede).toHaveText(text);
    const type = await lede.evaluate((el) => {
      const style = getComputedStyle(el);
      return { size: style.fontSize, weight: style.fontWeight };
    });
    expect(type.size).toBe("15px");
    expect(type.weight).toBe("600");
  }

  async function expectNoHorizontalScroll(page) {
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - window.innerWidth,
    );
    expect(overflow).toBeLessThanOrEqual(0);
  }

  // Chromium reports `env(safe-area-inset-top)` as 0 on every machine without a
  // notch, so the inset is injected where the shell reads it: a custom property
  // on the document element. 47px is the value a notched iPhone reports. With
  // the sticky bar at `top: var(--safe-top)` (1.10.0) the bar is painted 47px
  // lower while its flow box stays put, covering the intro completely: the
  // bar's bottom lands *below* the lede's top. `padding-top` (1.10.1) grows
  // the bar instead, so the flow moves with it. Both facts are measured below
  // rather than deduced from a screenshot.
  async function expectSafeAreaDoesNotCoverLede(page) {
    await expect(page.locator(".lede")).toBeVisible();
    await page.evaluate(() => {
      document.documentElement.style.setProperty("--safe-top", "47px");
      window.scrollTo(0, 0);
    });

    const measured = await page.evaluate(() => {
      const bar = document.querySelector(".topbar").getBoundingClientRect();
      const ledeEl = document.querySelector(".lede");
      const lede = ledeEl.getBoundingClientRect();
      const hit = document.elementFromPoint(
        lede.left + lede.width / 2,
        lede.top + lede.height / 2,
      );
      return {
        barBottom: bar.bottom,
        ledeTop: lede.top,
        ledeHit: Boolean(hit?.closest(".lede")),
      };
    });

    expect(measured.ledeTop).toBeGreaterThanOrEqual(measured.barBottom);
    expect(measured.ledeHit).toBe(true);
    await expectNoHorizontalScroll(page);
  }

  test("the request book shows every view and a typed intro", async ({
    page,
  }) => {
    await mock(page);
    await page.goto("/");
    await expect(page.locator(".tbl-row")).toHaveCount(5);
    await expectTabs(page, "Requests");
    await expectLede(page, "The request book");
    await expectNoHorizontalScroll(page);
  });

  test("plan types shows every view and a typed intro", async ({ page }) => {
    await mock(page);
    await page.goto("/plan-types");
    await expect(page.locator(".type-card")).toHaveCount(2);
    await expectTabs(page, "Plan types");
    await expectLede(page, "Plan types");
    await expectNoHorizontalScroll(page);
  });

  test("datasets shows every view and a typed intro", async ({ page }) => {
    await mock(page);
    await page.goto("/datasets");
    await expect(page.locator(".card")).toHaveCount(2);
    await expectTabs(page, "Datasets");
    await expectLede(page, "Datasets");
    await expectNoHorizontalScroll(page);
  });

  test("on a notched home screen the bar does not eat the intro", async ({
    page,
  }) => {
    await mock(page);
    await page.goto("/");
    await expect(page.locator(".tbl-row")).toHaveCount(5);
    await expectSafeAreaDoesNotCoverLede(page);
  });

  test("the plan-types intro also clears a notched bar", async ({ page }) => {
    await mock(page);
    await page.goto("/plan-types");
    await expect(page.locator(".type-card")).toHaveCount(2);
    await expectSafeAreaDoesNotCoverLede(page);
  });
});

test.describe("the page ends where the content ends", () => {
  // The footer used to hold a generated-at stamp, the store path, and a link
  // to the other tab. The link duplicated the nav directly above it, the
  // timestamp always read "now" on a page that is never cached, and the path
  // is the host's business. A page that is one list should stop after the list.
  for (const path of ["/", "/datasets", "/plan-types"]) {
    test(`${path} has no footer and no server trivia`, async ({ page }) => {
      await mock(page);
      await page.goto(path);

      // A `<footer>` on a page is a page footer, and there is none. This is
      // also what keeps a card's own landed-at line (the datasets browser has
      // one) a `div`: card metadata is not a page footer, and a `<footer>`
      // inside a card would make this count 2 on the third view.
      await expect(page.locator("footer")).toHaveCount(0);
      const body = await page.locator("body").innerText();
      expect(body).not.toContain("generated");
      expect(body).not.toContain("read-only, no authority");
      expect(body).not.toContain("data-download.sqlite");

      // The navigation exists once, in the bar.
      await expect(page.locator(".main-nav")).toHaveCount(1);
      await expect(page.locator(".main-nav a")).toHaveCount(3);
    });
  }
});

test.describe("naming a request, and opening it", () => {
  test("a row leads with a name, not with the key", async ({ page }) => {
    await mock(page);
    await page.goto("/");

    const first = page.locator(".tbl-row").first();
    // `#1` and what was asked for. The 32-hex key is the tooltip and nothing
    // else in the row: it is a durable handle, and a handle is not a name.
    await expect(first.locator(".req-num")).toHaveText("#1");
    await expect(first.locator(".req-subject")).toHaveText(
      "databento_batch_download",
    );
    await expect(first.locator(".req-id")).toHaveCount(0);
    await expect(first).not.toContainText("9f2a41c8");
    // Who is being asked. A dataset is vendor vocabulary; the name is context.
    await expect(first.locator(".req-vendor")).toHaveText("Databento");

    const link = first.locator("a.req");
    await expect(link).toHaveAttribute("href", /\/requests\/req_9f2a41c8$/);
    await expect(link).toHaveAttribute("title", "req_9f2a41c8");
  });

  test("an intent is named by what it asked for, not by its type", async ({
    page,
  }) => {
    await mock(page);
    await page.goto("/");
    const intentRow = page.locator(".tbl-row").filter({
      hasText: "OPRA.PILLAR · trades",
    });
    await expect(intentRow.locator(".req-subject")).toHaveText(
      "OPRA.PILLAR · trades · 2026-10-01→2026-10-08",
    );
    // An intent has no plan type, so its vendor comes from the registry's
    // dataset namespace — the same word the plan rows show.
    await expect(intentRow.locator(".req-vendor")).toHaveText("Databento");
  });

  test("the whole row is the target, not just the name", async ({ page }) => {
    await mock(page);
    await page.goto("/");

    // The far end of the row -- the badge in the last column -- which used to be
    // dead space. The anchor is still the only link; its `::after` covers the row.
    const row = page.locator(".tbl-row").filter({ hasText: "OPRA.PILLAR" });
    // A raw mouse click at the far end of the row -- the last cell -- because a
    // locator click would (correctly) refuse: the point now belongs to the
    // row-wide link, not to the badge it happens to be over.
    const box = await row.boundingBox();
    await page.mouse.click(box.x + box.width - 12, box.y + box.height / 2);

    await expect(page).toHaveURL(/\/requests\/req_0a7c9d33$/);
    await expect(page.locator(".detail-title")).toContainText("#4 ·");
  });

  test("clicking a row opens the request, with the why and the trail", async ({
    page,
  }) => {
    await mock(page);
    await page.goto("/");

    await page
      .locator(".tbl-row")
      .filter({ hasText: "OPRA.PILLAR · trades" })
      .locator("a.req")
      .click();
    await expect(page).toHaveURL(/\/requests\/req_0a7c9d33$/);

    await expect(page.locator(".detail-title")).toHaveText(
      "#4 · OPRA.PILLAR · trades · 2026-10-01→2026-10-08",
    );
    await expect(page.locator(".detail-header .pill")).toHaveText("Blocked");

    // The two things the list could not show: the human sentence, and the key
    // in full, because this is the page where someone copies it into a curl.
    await expect(page.locator(".why")).toHaveText(
      "OPRA.PILLAR trades for the Oct expiry cycle",
    );
    await expect(page.locator(".meta-grid")).toContainText("req_0a7c9d33");

    // The append-only trail, which no list view has room for.
    await expect(page.locator(".events li")).toHaveCount(1);
    await expect(page.locator(".events li")).toContainText("submitted");

    // The body the caller sent, verbatim, behind a disclosure.
    await page.locator("details.raw summary").click();
    await expect(page.locator("details.raw pre")).toContainText("OPRA.PILLAR");
  });

  test("the request is read before the decision about it", async ({ page }) => {
    await mock(page, { detail: draftDetail });
    await page.goto("/requests/req_draft01");

    // The order is the argument: what was asked for, then why, then the verb.
    // "what was asked for" used to sit third, under the approve card -- the
    // reader got the decision before the thing being decided about.
    const order = await page
      .locator(".detail-card .block")
      .evaluateAll((blocks) =>
        blocks.map((block) =>
          block.querySelector(".micro")?.textContent.trim(),
        ),
      );
    expect(order.slice(0, 3)).toEqual(["what was asked for", "why", "spend"]);

    const request = page.locator(".block.request");
    // A request as a person says it: the list is a list and the window is a
    // range, not two JSON blobs shown one key at a time.
    await expect(request).toContainText("GLBX.MDP3");
    await expect(request).toContainText("ES.c.0");
    await expect(request).toContainText("2026-11-04 → 2026-11-06");
    await expect(request).not.toContainText('{"start"');

    // And the money, as rows in the same grid -- `quoted` and `ceiling` are
    // fields of this request exactly as `window` and `stype_in` are. Two
    // columns, not a right-aligned block that looked like a different kind of
    // fact: the label sits where every other label sits, and so does the value.
    const cells = await request.locator(".spec").evaluate((grid) => {
      const rows = [...grid.children];
      const found = {};
      for (let i = 0; i < rows.length; i += 2) {
        found[rows[i].textContent.trim()] = {
          label: rows[i].getBoundingClientRect().left,
          value: rows[i + 1].getBoundingClientRect().left,
        };
      }
      return found;
    });
    expect(cells.quoted.value).toBeGreaterThan(0);
    for (const key of ["window", "quoted", "ceiling"]) {
      expect(cells[key].label).toBeCloseTo(cells.window.label, 0);
      expect(cells[key].value).toBeCloseTo(cells.window.value, 0);
    }
    await expect(request.locator(".spec")).toContainText("$0.8115");
    await expect(request.locator(".spec")).toContainText("$5");
  });

  test("an unpriced request says which kind of unpriced it is", async ({
    page,
  }) => {
    // The same row with no quote on it -- a blocked intent, which has no Job.
    await mock(page, {
      detail: {
        ...draftDetail,
        estimate_usd: null,
        blocked_reason: "awaiting_plan",
      },
    });
    await page.goto("/requests/req_draft01");
    // A dash would make three different absences look like one, and an
    // unpriced request look like a free one.
    await expect(page.locator(".block.request .unpriced")).toHaveText(
      "not priced — there is no job to quote",
    );
  });

  test("the trail says who only when that is not the event's own word", async ({
    page,
  }) => {
    await mock(page, {
      detail: {
        ...draftDetail,
        events: [
          draftDetail.events[0],
          {
            id: 2,
            ts: "2026-11-15T23:50:00Z",
            actor_kind: "human",
            event: "approved",
            detail: { actor: "nick@example.com", note: "go on then" },
          },
        ],
      },
    });
    await page.goto("/requests/req_draft01");

    const rows = page.locator(".events li");
    await expect(rows).toHaveCount(2);
    // `submitted` followed by `submitter` was the same word twice. No identity
    // is recorded, so there is no third column to print.
    await expect(rows.nth(0).locator(".what")).toHaveText("submitted");
    await expect(rows.nth(0).locator(".who")).toHaveCount(0);
    // An approval is worth a name, and the name is the login the tailnet
    // asserted -- not the word "human".
    await expect(rows.nth(1).locator(".who")).toHaveText("nick@example.com");
  });

  test("the detail page is a page: a reload and a back link both work", async ({
    page,
  }) => {
    await mock(page);
    await page.goto("/requests/req_0a7c9d33");
    await expect(page.locator(".detail-title")).toContainText("#4 ·");
    await page.reload();
    await expect(page.locator(".detail-title")).toContainText("#4 ·");
    await expect(page.locator(".back-btn")).toBeVisible();
  });
});

test.describe("approving a request", () => {
  // The page used to have no verb at all. It has exactly one now, and it is the
  // one that spends money. It asked for an `approve` token and a name; it now
  // asks the server who the tailnet hop says you are, so the card can neither
  // leak a secret nor be handed one.
  const noteField = (page) => page.locator('.approve input[type="text"]');
  const approveButton = (page) => page.locator(".approve .btn.primary");

  async function interceptApprove(page, status = 200, body = null) {
    const seen = [];
    await page.route("**/v1/requests/**/approve", async (route) => {
      seen.push({
        headers: route.request().headers(),
        body: route.request().postDataJSON(),
      });
      await route.fulfill({
        status,
        contentType: "application/json",
        body: JSON.stringify(
          body ?? {
            request_id: "req_draft01",
            state: "approved",
            job_hash: "a3d57f9b",
            approved: true,
          },
        ),
      });
    });
    return seen;
  }

  test("the card names the approver and asks for nothing", async ({ page }) => {
    await mock(page, { detail: draftDetail });
    await page.goto("/requests/req_draft01");

    await expect(page.locator(".approve .who")).toContainText(
      "Approving as nick@example.com.",
    );
    // No credential field and no name field: both would be a second identity.
    await expect(page.locator('.approve input[type="password"]')).toHaveCount(
      0,
    );
    await expect(page.locator(".approve .fields label")).toHaveCount(1);
    await expect(approveButton(page)).toBeEnabled();
    await expect(approveButton(page)).toContainText("$5");
  });

  test("the numbers are read before the click, and one click is the spend", async ({
    page,
  }) => {
    const seen = await interceptApprove(page);
    await mock(page, { detail: draftDetail });
    await page.goto("/requests/req_draft01");

    // What is being authorised is on the page before anybody clicks: the quote
    // the vendor gave for this Job and the ceiling the approval authorises.
    // It used to be a second screen between two clicks -- a layer of approval
    // asking the question the first click had already asked.
    const ask = page.locator(".approve .ask");
    await expect(ask).toContainText(
      "The vendor quoted $0.8115 for exactly this Job.",
    );
    await expect(ask).toContainText("Approving authorises up to $5.");
    expect(seen).toHaveLength(0);

    await noteField(page).fill("ES two days");
    await approveButton(page).click();

    await expect.poll(() => seen.length).toBe(1);
    // No credential travels: the route would refuse one anyway.
    expect(seen[0].headers.authorization).toBeUndefined();
    expect(seen[0].body).toEqual({ note: "ES two days" });
    await expect(page).not.toHaveURL(/token=/);
  });

  test("a request with no ceiling says so rather than inventing one", async ({
    page,
  }) => {
    await mock(page, {
      detail: { ...draftDetail, max_usd: null },
    });
    await page.goto("/requests/req_draft01");

    // The card said "up to the ceiling it carries" on a request that carries
    // no ceiling -- a sentence that reads as a bound where there is none.
    const ask = page.locator(".approve .ask");
    await expect(ask).not.toContainText("ceiling it carries");
    await expect(ask).not.toContainText("up to");
    await expect(ask).toContainText(
      "This request carries no ceiling, so nothing here bounds what it may cost.",
    );
    await expect(ask).toContainText("quoted $0.8115");
    await expect(approveButton(page)).toHaveText("Approve");
    // Sentences, not words run together: the card used to render
    // "…?The vendor quoted…" with no space after the question mark.
    const said = (await ask.textContent()).replace(/\s+/g, " ");
    expect(said).toContain("Job. This request carries no ceiling");
    expect(said).toContain("may cost. Nothing is downloaded here");
  });

  test("the token this card used to ask for is scrubbed from the browser", async ({
    page,
  }) => {
    await mock(page, { detail: draftDetail });
    await page.goto("/requests/req_draft01");
    await page.evaluate(() => {
      window.localStorage.setItem("data-download.approve-token", "tok-approve");
      window.localStorage.setItem("data-download.actor", "nick");
    });
    await page.reload();

    // A secret left in storage after its door was removed reads as live. The
    // page that asked for it is the page that deletes it.
    const left = await page.evaluate(() => [
      window.localStorage.getItem("data-download.approve-token"),
      window.localStorage.getItem("data-download.actor"),
    ]);
    expect(left).toEqual([null, null]);
  });

  test("a refusal is the server's, in the server's words", async ({ page }) => {
    await interceptApprove(page, 403, {
      error: {
        code: "not_an_approver",
        message:
          "this tailnet login is not named in DATA_DOWNLOAD_APPROVERS, " +
          "so it may read but not approve",
      },
    });
    await mock(page, { detail: draftDetail });
    await page.goto("/requests/req_draft01");

    await approveButton(page).click();

    await expect(page.locator(".approve .failure")).toContainText(
      "not named in DATA_DOWNLOAD_APPROVERS",
    );
  });

  test("a login that may read but not spend gets no button", async ({
    page,
  }) => {
    await mock(page, {
      detail: draftDetail,
      who: { login: "someone-else@example.com", approver: false },
    });
    await page.goto("/requests/req_draft01");

    await expect(page.locator(".approve .who")).toContainText(
      "Approving as someone-else@example.com.",
    );
    await expect(page.locator(".approve .who")).toContainText(
      "may read but not approve",
    );
    await expect(approveButton(page)).toHaveCount(0);
  });

  test("a page that did not come through serve offers no verb", async ({
    page,
  }) => {
    await mock(page, { detail: draftDetail, whoStatus: 403 });
    await page.goto("/requests/req_draft01");

    // Not a disabled button: nothing here could be fixed by clicking, so the
    // only honest thing to render is why.
    await expect(approveButton(page)).toHaveCount(0);
    await expect(page.locator(".approve .failure")).toContainText(
      "no Tailscale-User-Login header",
    );
  });

  test("an approved row has nothing to click", async ({ page }) => {
    await mock(page, {
      detail: { ...draftDetail, state: "approved", approved_unclaimed: true },
    });
    await page.goto("/requests/req_draft01");
    await expect(page.locator(".approve")).toHaveCount(0);
    await expect(page.locator(".detail-header .pill")).toHaveText("Approved");
    // The book's sentence, in full, on the page it was clicked through to. A
    // different vocabulary here would be a second answer to one question.
    await expect(page.locator(".meta-grid")).toContainText("worker");
    await expect(page.locator(".meta-grid")).toContainText(
      "none — no worker has picked it up",
    );
  });

  test("a blocked row has nothing to click either", async ({ page }) => {
    await mock(page);
    await page.goto("/requests/req_0a7c9d33");
    await expect(page.locator(".approve")).toHaveCount(0);
  });
});

test.describe("the bar at the top of the page", () => {
  // This describe block used to be "the bar as a Home Screen app". app-shell's
  // bar was glass, so this app patched it under `display-mode: standalone` and
  // under `html.ios-standalone` (set inline in index.html from
  // `navigator.standalone`). @nb 1.16.1 made the bar opaque everywhere -- a bar
  // that owns the top edge of the viewport has nothing above it to see through
  // to -- and the patch is gone with it.
  //
  // So this asserts the block's own rule on an ordinary page load, with nothing
  // emulated, because that is now the only case there is. The regression it
  // guards has not gone away, it has moved: it is a consumer stylesheet putting
  // the glass back for specificity, which is precisely what the deleted patch
  // did in the other direction.
  for (const [path, name] of [
    ["/", "the request book"],
    ["/plan-types", "plan types"],
  ]) {
    test(`${name}: the bar is opaque, with no blur`, async ({ page }) => {
      await mock(page);
      await page.goto(path);

      // Only the standard property: Chromium does not serialise the
      // `-webkit-` alias in computed style, so asserting on it proves nothing.
      const bar = await page.locator(".topbar").evaluate((el) => {
        const style = getComputedStyle(el);
        return {
          backdrop: style.backdropFilter,
          background: style.backgroundColor,
        };
      });

      expect(bar.backdrop).toBe("none");
      // `background` is a var(--bg) shorthand, so an opaque colour is the whole
      // assertion: a percentage here is the bug coming back.
      expect(bar.background).toBe("rgb(15, 23, 42)");
    });
  }

  test("it is the same while content is scrolling under it", async ({
    page,
  }) => {
    await mock(page);
    await page.goto("/");
    await page.evaluate(() => window.scrollTo(0, 600));
    await expect
      .poll(() =>
        page
          .locator(".topbar")
          .evaluate((el) => getComputedStyle(el).backgroundColor),
      )
      .toBe("rgb(15, 23, 42)");
    const backdrop = await page
      .locator(".topbar")
      .evaluate((el) => getComputedStyle(el).backdropFilter);
    expect(backdrop).toBe("none");
  });
});

// One blocked row used to carry one label. It carried three different
// situations with three different next moves, and a reader could not tell "add
// two fields" from "this needs new code" — the question that started phase 2.5.
test.describe("a request that is waiting, in three different ways", () => {
  test("the facets name who moves next, not the state", async ({ page }) => {
    await mock(page, { requests: blockedBook });
    await page.goto("/");

    const group = page.locator(".facet-group").first();
    // Each blocked answer is its own choice, one row apiece.
    for (const [label, count] of [
      ["Needs parameters", "1"],
      ["No plan yet", "1"],
      ["Plan requested", "1"],
    ]) {
      const option = group
        .locator(".facet-option")
        .filter({ hasText: label })
        .first();
      await expect(option).toBeVisible();
      await expect(option).toContainText(count);
    }
    // The old name is gone rather than aliased: two words for one facet would
    // be two filters for one set of rows.
    await expect(page.locator("body")).not.toContainText("Waiting on a plan");
  });

  test("each blocked row says what would clear it", async ({ page }) => {
    await mock(page, { requests: blockedBook });
    await page.goto("/");

    const row = (seq) =>
      page.locator(".tbl-row").filter({ hasText: `#${seq}` });
    // All three are `blocked`, so all three badges say the same word; the line
    // under it is what tells them apart, and it is the only place on the page
    // that says what would clear the row.
    for (const seq of [1, 2, 3]) {
      await expect(row(seq).locator(".pill")).toHaveText("Blocked");
    }
    // The caller's move: the two params the plan is short of.
    await expect(row(1).locator(".state .detail")).toHaveText(
      "add symbols, stype_in",
    );
    // No registered type speaks for the dataset. Nothing the caller can add.
    await expect(row(2).locator(".state .detail")).toHaveText(
      "no plan for ZZZZ.UNKNOWN",
    );
    // The ask is in, so the row is no longer "no plan yet".
    await expect(row(3).locator(".state .detail")).toHaveText(
      "new plan requested",
    );
    // The timestamp is the tooltip, not a second line: the detail is what
    // clears the row, and the row already has a `filed` column.
    await expect(row(3).locator(".state .detail")).toHaveAttribute(
      "title",
      /asked for on 14 Nov/,
    );
  });

  test("filtering on one of them narrows to exactly that row", async ({
    page,
  }) => {
    await mock(page, { requests: blockedBook });
    await page.goto("/?show=no_plan");
    await expect(page.locator(".tbl-row")).toHaveCount(1);
    await expect(page.locator(".tbl-row")).toContainText("#2");
    await expect(page).toHaveURL(/show=no_plan/);
  });

  test("the request page shows the gap and the example that would fit", async ({
    page,
  }) => {
    await mock(page);
    await page.goto("/requests/req_0a7c9d33");

    const section = page
      .locator(".block")
      .filter({ hasText: "why there is no job" });
    // The verdict, in the server's vocabulary: `needs_params` is the thing the
    // caller has to close, and it is a word they will see again in the API.
    await expect(section.locator(".pill")).toContainText(["needs_params"]);
    await expect(section).toContainText("OPRA.PILLAR");
    // Every registered type answers, so an absent candidate is not a silence
    // a reviewer has to go looking behind.
    await expect(section.locator(".candidate").first()).toContainText(
      "databento_batch_download",
    );
    await expect(section.locator(".candidate").first()).toContainText(
      "missing symbols, stype_in",
    );

    // The closest fit: the caller's own params kept, the gaps as placeholders,
    // and the server's own caveat that this is an example, not a quote.
    const would = section.locator(".would-fit");
    await expect(would).toContainText("the closest fit");
    await expect(would).toContainText("OPRA.PILLAR");
    await expect(would).toContainText("<symbols>");
    await expect(would).toContainText("an example shape, not a quote");

    // No "ask for a new plan" line here: `needs_params` is the caller's own
    // gap, the shorter path is theirs to take, and the service refuses the ask
    // while a plan merely wants two more fields.
    await expect(section).not.toContainText("escalate");
    await expect(section).not.toContainText("POST /v1/requests/");
  });

  test("a row nobody has asked about names the escalate door; one already asked does not", async ({
    page,
  }) => {
    // `needs_params` is never escalated: the caller has a shorter path, and the
    // service refuses the ask. So the line that names the door is for the row
    // that has no plan at all.
    await mock(page, {
      detail: {
        ...requestDetail,
        facet: "no_plan",
        facet_label: "No plan yet",
        match: {
          ...requestDetail.match,
          verdict: "not_claimed",
          plan_type: null,
          candidates: [],
          would_fit: undefined,
        },
      },
    });
    await page.goto("/requests/req_0a7c9d33");
    const section = page
      .locator(".block")
      .filter({ hasText: "why there is no job" });
    await expect(section).toContainText("escalate");
    await expect(section).toContainText("scope");

    // Already asked: the row has a date, and the door is not offered twice.
    await mock(page, {
      detail: {
        ...requestDetail,
        facet: "requested",
        facet_label: "Plan requested",
        escalated_at: "2026-11-14T08:15:00Z",
        match: { ...requestDetail.match, verdict: "not_claimed" },
      },
    });
    await page.goto("/requests/req_0a7c9d33");
    await expect(section).toContainText("new plan asked for");
    await expect(section).not.toContainText("POST /v1/requests/");
  });
});

test.describe("the datasets browser", () => {
  test("is vendor, then dataset, then what landed", async ({ page }) => {
    await mock(page);
    await page.goto("/datasets");

    // The vendor is the top level, because a dataset code is vendor
    // vocabulary: `GLBX.MDP3` under `databento` is the same fact as the path
    // on disk.
    await expect(page.locator(".vendor h2")).toHaveText("databento");
    await expect(page.locator(".vendor h3")).toHaveText("GLBX.MDP3");
    await expect(page.locator(".card")).toHaveCount(2);

    const landed = page.locator(".card").nth(0);
    await expect(landed.locator(".pill")).toHaveText("Succeeded");
    await expect(landed.locator(".facts")).toContainText(
      "2026-11-04 → 2026-11-06",
    );
    await expect(landed.locator(".facts")).toContainText(
      "trades · ES.c.0 · continuous",
    );
    await expect(landed.locator(".files li")).toHaveCount(2);
    await expect(landed.locator(".files a").first()).toHaveAttribute(
      "href",
      /^\/datasets\/file\/databento\/GLBX\.MDP3\/req_landed01\//,
    );
    // A download, not a navigation: a phone should save the file.
    await expect(landed.locator(".files a").first()).toHaveAttribute(
      "download",
      "",
    );
    // The card describes the landing and nothing else: no marker about which
    // driver produced it. That belongs to `landing.json` and `/ui/datasets.json`,
    // where it is a fact to read, not a badge to scroll past.
    await expect(page.locator(".stub")).toHaveCount(0);
    await expect(landed.locator(".card-foot")).toContainText(
      "sha c69889effb5d",
    );
  });

  test("a half-finished download shows the files it already has", async ({
    page,
  }) => {
    await mock(page);
    await page.goto("/datasets");

    const running = page.locator(".card").nth(1);
    await expect(running.locator(".pill")).toHaveText("Running");
    // The whole reason this page exists next to the request book: a run with
    // one of four days on disk must look like progress, not like nothing.
    await expect(running.locator(".files li")).toHaveCount(1);
    await expect(running.locator(".files li")).toContainText(
      "GLBX.MDP3-2026-11-04.dbn.zst",
    );
    // Not settled yet, so no landing digest and no verdict to claim.
    await expect(running.locator(".card-foot")).not.toContainText("sha");
  });

  test("says what a summary is worth, and keeps polling while a run moves", async ({
    page,
  }) => {
    let calls = 0;
    await page.route("**/ui/datasets.json", (route) => {
      calls += 1;
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(datasets),
      });
    });
    await page.goto("/datasets");

    await expect(page.locator(".summary")).toContainText("1 vendor");
    await expect(page.locator(".summary")).toContainText("3 files");
    await expect(page.locator(".summary")).toContainText("1 in flight");

    await expect.poll(() => calls, { timeout: 12000 }).toBeGreaterThan(1);
  });

  test("a missing datasets root is a sentence, not an empty list", async ({
    page,
  }) => {
    await mock(page, {
      tree: { root: null, configured: false, vendors: [], totals: {} },
    });
    await page.goto("/datasets");

    await expect(page.locator(".empty")).toContainText(
      "No datasets root is configured",
    );
    await expect(page.locator(".card")).toHaveCount(0);
  });

  test("is one tap from the request book", async ({ page }) => {
    await mock(page);
    await page.goto("/");
    await page.getByRole("link", { name: "Datasets" }).click();
    await expect(page).toHaveURL(/\/datasets$/);
    await expect(page.locator(".lede")).toHaveText("Datasets");
  });
});
