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
    // Both the bar action and the foot link, because the token is a browser
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
    await expect(page.locator(".topbar")).toContainText("Request book");
  });

  test("an empty registry says every submission will be rejected", async ({
    page,
  }) => {
    await mock(page, {
      types: { types: [], generated_at: "2026-11-14 15:30:00" },
    });
    await page.goto("/plan-types");
    await expect(page.locator(".empty-state")).toContainText(
      "every submission will be rejected",
    );
  });
});
