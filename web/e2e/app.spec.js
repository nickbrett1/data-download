import { expect, test } from "@playwright/test";
import { dashboard, planTypes } from "./fixtures.js";

// Resource-type routing, not a URL glob: `**/stats` would also match the
// document navigation and hand the browser JSON instead of the app.
async function mock(page, { requests, types } = {}) {
  await page.route(
    "**/ui/**",
    (route) => {
      const url = route.request().url();
      const body = url.includes("plan-types")
        ? (types ?? planTypes)
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
    await expect(page.locator(".tbl-head .col-label")).toHaveText([
      "request",
      "state",
      "type",
      "estimate",
      "age",
      "why it matters",
    ]);

    // Six columns of a grid share one template, which is what makes `$0.0412`
    // sit under `$0.0412` rather than wherever the previous cell ended.
    const template = await page
      .locator(".tbl-row")
      .first()
      .evaluate((el) => getComputedStyle(el).gridTemplateColumns);
    expect(template.split(" ").length).toBe(6);

    const first = page.locator(".tbl-row").first();
    await expect(first).toContainText("$0.0412");
    await expect(first).toContainText("1h 4m");
    await expect(first.locator(".pill")).toHaveText("succeeded");
  });

  test("a stuck condition outranks the state it is stuck in", async ({
    page,
  }) => {
    await mock(page);
    await page.goto("/");

    const running = page
      .locator(".tbl-row")
      .filter({ hasText: "req_4c81be07" });
    // The row's state is `running`; the badge says why it is worth looking at.
    await expect(running.locator(".pill").first()).toHaveText("running");
    await expect(running.locator(".pill.bad")).toHaveText("lease_expired");
    await expect(running.locator(".pill.warn")).toHaveCount(0);
  });

  test("the facet is one choice from N, drawn as a histogram", async ({
    page,
  }) => {
    await mock(page);
    await page.goto("/");

    const group = page.locator(".facet-group").first();
    const radios = group.locator('input[type="radio"]');
    await expect(radios).toHaveCount(6);
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

    await page
      .locator(".facet-option")
      .filter({ hasText: "To approve" })
      .click();
    await expect(page).toHaveURL(/show=todo/);
    await expect(page.locator(".tbl-row")).toHaveCount(1);
    await expect(page.locator(".tbl-row")).toContainText("req_b31d5e0a");

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
    await expect(page.locator(".tbl-row")).toContainText("req_4c81be07");
  });

  test("the token survives the link between the two pages", async ({
    page,
  }) => {
    await mock(page);
    await page.goto("/?token=tok-read");
    // Both the nav tab and the foot link, because the token is a browser
    // navigation in each case and neither can carry a header.
    const links = page.locator('a[href="/plan-types?token=tok-read"]');
    expect(await links.count()).toBeGreaterThan(0);
  });

  test("an unauthenticated answer says what to do about it", async ({
    page,
  }) => {
    await page.route(
      "**/ui/**",
      (route) => route.fulfill({ status: 401, body: "" }),
      { times: 99 },
    );
    await page.goto("/");
    await expect(page.locator(".empty-state")).toContainText("?token=");
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
    // and the wordmark is the page title, so a reader knows where they are
    await expect(page.locator(".topbar")).toContainText("data-download");
    await expect(page.locator(".topbar")).toContainText("the request book");
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

  test("the prose a schema cannot say itself is still there", async ({
    page,
  }) => {
    await mock(page);
    await page.goto("/plan-types");

    const first = page.locator(".type-card").first();
    await expect(first.locator(".kv")).toContainText("Rejected");
    await expect(first.locator(".kv")).toContainText("retry");
    await expect(first.locator(".kv")).toContainText(
      "Resume from the last landed batch.",
    );
    await expect(first.locator("details pre")).toContainText("draft/2020-12");

    // `closed: false` is the other branch, and it must not read as rejection.
    await expect(
      page.locator(".type-card").nth(1).locator(".kv"),
    ).toContainText("Accepted");
    // No on_failure verbs is "nothing permitted", not an empty list.
    await expect(
      page.locator(".type-card").nth(1).locator(".kv"),
    ).toContainText("nothing permitted");
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
    await expect(tabs).toHaveCount(2);
    await expect(tabs).toHaveText(["Requests", "Plan types"]);
    // Exactly one tab is the current page, and it is the right one.
    await expect(page.locator('.main-nav a[aria-current="page"]')).toHaveCount(
      1,
    );
    await expect(page.locator('.main-nav a[aria-current="page"]')).toHaveText(
      current,
    );
    // Both tabs are reachable without scrolling the bar: a tab row that hides
    // the second view is the bug this test exists for.
    const fits = await page
      .locator(".main-nav")
      .evaluate((el) => el.scrollWidth <= el.clientWidth + 1);
    expect(fits).toBe(true);
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

  test("the request book shows both views and a typed intro", async ({
    page,
  }) => {
    await mock(page);
    await page.goto("/");
    await expect(page.locator(".tbl-row")).toHaveCount(5);
    await expectTabs(page, "Requests");
    await expectLede(page, "The request book");
    await expectNoHorizontalScroll(page);
  });

  test("plan types shows both views and a typed intro", async ({ page }) => {
    await mock(page);
    await page.goto("/plan-types");
    await expect(page.locator(".type-card")).toHaveCount(2);
    await expectTabs(page, "Plan types");
    await expectLede(page, "Plan types");
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
  for (const path of ["/", "/plan-types"]) {
    test(`${path} has no footer and no server trivia`, async ({ page }) => {
      await mock(page);
      await page.goto(path);

      await expect(page.locator("footer")).toHaveCount(0);
      const body = await page.locator("body").innerText();
      expect(body).not.toContain("generated");
      expect(body).not.toContain("read-only, no authority");
      expect(body).not.toContain("data-download.sqlite");

      // The navigation exists once, in the bar.
      await expect(page.locator(".main-nav")).toHaveCount(1);
      await expect(page.locator(".main-nav a")).toHaveCount(2);
    });
  }
});
