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

// The plan-types fixture with one vendor's build replaced. `null` clears it, so
// a test can say what a vendor nobody has asked for looks like.
function withBuildAt(index, build) {
  return {
    ...planTypes,
    types: planTypes.types.map((type, i) =>
      i === index
        ? {
            ...type,
            implementations: type.implementations.map((impl) => ({
              ...impl,
              build,
            })),
          }
        : type,
    ),
  };
}

test.describe("the request book", () => {
  test("is one list with a badge per row, and the figures line up", async ({
    page,
  }) => {
    await mock(page);
    await page.goto("/");

    await expect(page.locator(".tbl-row")).toHaveCount(5);
    // Five columns: what it is, the window it covers, where it is, what it
    // costs, when it was asked for. "why it matters" was the one removed -- on
    // a finished row it either repeated the badge or rendered a dash, and its
    // useful half (what a blocked row is short of) is a qualification of the
    // state, so it sits under the badge now.
    await expect(page.locator(".tbl-head .col-label")).toHaveText([
      "request",
      "window",
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
    expect(template.split(" ").length).toBe(5);

    const first = page.locator(".tbl-row").first();
    // Phase 4: the quote is on the row, at the precision a purchase needs.
    await expect(first.locator(".quote")).toHaveText("$0.04");
    // When it was filed, in a form a person reads. Not "1h 4m": an age answers
    // a question nobody asked and has to be converted back into a clock.
    await expect(first.locator(".filed")).toHaveText("14 Nov 23:10");
    await expect(first.locator(".pill")).toHaveText("Succeeded");
  });

  test("says which days each request covers", async ({ page }) => {
    await mock(page);
    await page.goto("/");

    // The window is the one qualification a reader compares between rows, and
    // it used to be invisible: it lives in `params_json`, the column the book
    // drops, so the page could not draw it at all.
    const first = page.locator(".tbl-row").first();
    // When it opens, and how long it runs. The end date is the weaker half of
    // the pair -- it is `start + days` -- and the length is what the quote
    // scales with, so the length is what the cell prints.
    await expect(first.locator(".window")).toHaveText("2026-10-01 · 7 days");
    // The whole range, and the phrase that says which date is included (the
    // convention is the vendor's: `20 → 21` is one day), ride in the tooltip.
    await expect(first.locator(".window")).toHaveAttribute(
      "title",
      "2026-10-01 → 2026-10-08 (end exclusive)",
    );

    // One reading per row, from either body shape: a plan's flat `start`/`end`
    // and an intent's nested `window` arrive as the same window.
    await expect(page.locator(".tbl-row .window")).toHaveText([
      "2026-10-01 · 7 days",
      "2026-11-14 · 1 day",
      "2026-11-15 · 1 day",
      "2026-10-01 · 7 days",
      "2026-11-01 · 4 days",
    ]);
  });

  test("a row with no complete window says so with a dash", async ({
    page,
  }) => {
    // A draft whose params are still being assembled names a start and no end.
    // Printing half a range would invent the other half, and printing the start
    // alone reads as a window that ends the same day.
    const rows = dashboard.rows.map((row, index) =>
      index === 1 ? { ...row, window: null } : row,
    );
    await mock(page, { requests: { ...dashboard, rows } });
    await page.goto("/");

    const second = page.locator(".tbl-row").nth(1);
    await expect(second.locator(".window")).toHaveText("—");
    await expect(second.locator(".window")).toHaveAttribute("title", "");

    // A window whose dates are not plain days has no length, and the cell says
    // the range rather than a length it had to guess.
    const odd = dashboard.rows.map((row, index) =>
      index === 1
        ? {
            ...row,
            window: { start: "2026-11-14", end: "2026-11-15", days: null },
          }
        : row,
    );
    await mock(page, { requests: { ...dashboard, rows: odd } });
    await page.goto("/");
    await expect(page.locator(".tbl-row").nth(1).locator(".window")).toHaveText(
      "2026-11-14 → 2026-11-15",
    );
  });

  test("a row waiting on a driver says so, not that nobody has it", async ({
    page,
  }) => {
    // An approved row whose vendor has no driver: the worker that would claim it
    // has nothing to drive, so "no worker has it yet" is true and misleading.
    // `driver_state` was the answer, and no view read it.
    const rows = dashboard.rows.map((row, index) =>
      index === 2
        ? { ...row, driver_state: "building", approved_unclaimed: false }
        : row,
    );
    await mock(page, { requests: { ...dashboard, rows } });
    await page.goto("/");

    const waiting = page.locator(".tbl-row").nth(2);
    await expect(waiting.locator(".state .detail")).toHaveText(
      "waiting on a driver being built",
    );
    await expect(waiting.locator(".state .detail")).toHaveClass(/\binfo\b/);
  });

  test("a driver build that stopped is a fault, and says so", async ({
    page,
  }) => {
    const rows = dashboard.rows.map((row, index) =>
      index === 2
        ? { ...row, driver_state: "needs_human", approved_unclaimed: false }
        : row,
    );
    await mock(page, { requests: { ...dashboard, rows } });
    await page.goto("/");

    const stopped = page.locator(".tbl-row").nth(2).locator(".state .detail");
    await expect(stopped).toHaveText("driver build stopped — needs a human");
    await expect(stopped).toHaveClass(/\bbad\b/);
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

  test("on a phone the header row goes and every record keeps the same shape", async ({
    page,
  }) => {
    await mock(page);
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto("/");
    await expect(page.locator(".tbl-head")).toBeHidden();

    // Six columns on a phone is a scrollbar or a truncated id; the record wraps
    // instead, and stays a record. What it must also do is wrap the *same way*
    // every time: the block stacks a row with `flex-wrap`, which packs each
    // line greedily, so where a cell lands depends on how wide the text before
    // it happens to be -- two rows of one shape looked different because one
    // quote read `$1.94` and another `<$0.01`. The page lays its rows out on a
    // two-column grid instead: the subject, then window | state, then
    // quote | filed.
    const shapes = await page.locator(".tbl-row").evaluateAll((rows) =>
      rows.map((row) => {
        const top = row.getBoundingClientRect().top;
        const line = (selector) =>
          Math.round(
            row.querySelector(selector).getBoundingClientRect().top - top,
          );
        return {
          subject: line(".req"),
          window: line(".window"),
          state: line(".state"),
          quote: line(".quote"),
          filed: line(".filed"),
        };
      }),
    );

    const [first, ...rest] = shapes;
    for (const shape of rest) {
      expect(Math.abs(shape.window - first.window)).toBeLessThanOrEqual(2);
      expect(Math.abs(shape.subject - first.subject)).toBeLessThanOrEqual(2);
    }
    // ...and within a row, the pairs share a line, under the subject.
    for (const shape of shapes) {
      expect(Math.abs(shape.window - shape.state)).toBeLessThanOrEqual(3);
      expect(Math.abs(shape.quote - shape.filed)).toBeLessThanOrEqual(2);
      expect(shape.window - shape.subject).toBeGreaterThan(10);
      expect(shape.quote - shape.window).toBeGreaterThan(10);
    }
  });
});

test.describe("plan types", () => {
  // A fixed list, in order, not a picker: there are only a handful of plan
  // types by design, so everything is on the page and nothing has to be
  // selected before it can be read. A rail was tried and read as a filter.
  test("lists the plan types as sections, each with its drivers", async ({
    page,
  }) => {
    await mock(page);
    await page.goto("/plan-types");

    const sections = page.locator(".plan-type");
    await expect(sections.locator("h2")).toHaveText([
      "market_data_window",
      "http_fetch",
      "drivers with no plan type",
    ]);

    // The nesting is the model: a driver is inside a plan type, never beside
    // one. Its name is not a status on the heading.
    const first = sections.first();
    await expect(first.locator(".type-head h2")).toHaveText(
      "market_data_window",
    );
    await expect(first.locator(".driver-row")).toHaveCount(1);
    await expect(first.locator(".vendor")).toHaveText(["Databento"]);
    await expect(first.locator(".impl-on")).toHaveText(["databento v1"]);
    // Codes *and* when the driver was built: the count says how much the
    // driver claims to answer, the date says whether it is the one that was
    // just built. `filed` renders it in the reader's own timezone.
    await expect(first.locator(".driver-line .micro")).toHaveText([
      "3 dataset codes · built 24 Nov 16:37",
    ]);
    // Nothing is drawn beside the heading but the version: a status there
    // reads as an attribute of the name, which is how "market_data_window v1
    // driver" came to look like a driver called market_data_window.
    await expect(first.locator(".type-head .pill")).toHaveText(["v3"]);
    await expect(first.locator(".type-head .micro")).toHaveCount(0);
  });

  // A driver matches on two things, and the page says them as a rule: this
  // vendor, and one of these dataset codes.
  test("opens a driver to show what it matches", async ({ page }) => {
    await mock(page);
    await page.goto("/plan-types");

    const first = page.locator(".plan-type").first();
    // The newest installed artefact is open by default -- the common question
    // is "what is answering this?", not "is anything".
    await expect(first.locator(".driver-detail")).toContainText(
      "vendor Databento",
    );
    await expect(first.locator(".driver-detail")).toContainText("*.MDP3");
    // Two lines: what it matches, and the build that produced it. Not the
    // artefact restated -- its name and version are the row's pill, the
    // protocol number can only ever read 1 (a mismatch refuses discovery) and
    // an install date said when a file was copied, not what the driver does.
    // Where the code is: the artefact's directory in the driver repo, pinned
    // to the commit that was installed. A version number in the pill says
    // which artefact is running and nothing about where to read it.
    const code = first
      .locator(".driver-detail a.ref")
      .filter({ hasText: "drivers/databento/1/" });
    await expect(code).toHaveText("drivers/databento/1/");
    await expect(code).toHaveAttribute(
      "href",
      "https://github.com/nickbrett1/data-download-drivers/tree/9f28f7583a9b233802aa6d32df4b4dc6a563b1c7/drivers/databento/1",
    );
    const panel = first.locator(".driver-detail");
    await expect(panel).not.toContainText("24 Nov");
    // The line is named for the *build* now that the date lives on the row:
    // "built: installed" read as a contradiction when the date was not beside
    // it.
    await expect(panel.locator("dt").filter({ hasText: "build" })).toHaveCount(
      1,
    );

    const detail = await first.locator(".driver-detail").innerText();
    expect(detail).not.toMatch(/protocol/);
    expect(detail).not.toContain("databento v1");
    expect(detail).not.toMatch(/\binstalled 20/);

    // The row is the control, and it closes on a second click.
    const toggle = first.locator(".driver-toggle");
    await expect(toggle).toHaveAttribute("aria-expanded", "true");
    await toggle.click();
    await expect(first.locator(".driver-detail")).toHaveCount(0);
    await expect(toggle).toHaveAttribute("aria-expanded", "false");
    await toggle.click();
    await expect(first.locator(".driver-detail")).toContainText("*.MDP3");
  });

  test("a type with no driver says so, and cannot be opened", async ({
    page,
  }) => {
    // A vendor nobody has ever asked for: no job in the queue, so the row has
    // nothing to report but its own absence.
    const types = withBuildAt(1, null);
    await mock(page, { types });
    await page.goto("/plan-types");

    const pinned = page.locator(".plan-type").nth(1);
    await expect(pinned.locator("h2")).toHaveText("http_fetch");
    // A pinned seller IS the type's identity, so it is in the heading...
    await expect(pinned.locator(".type-head .pill")).toHaveText(["Nabu", "v1"]);
    // ...and the missing implementation is a row, not a pill on the heading.
    await expect(pinned.locator(".impl-off")).toHaveText([
      "no driver installed",
    ]);
    await expect(pinned.locator(".driver-toggle")).toBeDisabled();
  });

  // The other kind of request this service holds: a driver, asked for in the
  // queue rather than a dataset asked for from a vendor. Without it, a vendor
  // whose driver is being written reads exactly like one nobody will ever
  // write -- "wait" and "never", drawn identically.
  test("a driver being built says so, with the attempt it is on", async ({
    page,
  }) => {
    await mock(page);
    await page.goto("/plan-types");

    const pinned = page.locator(".plan-type").nth(1);
    // Not the dashed "nothing here": the row is in flight, and a dashed pill
    // would send the reader away from the one thing that is happening.
    await expect(pinned.locator(".build-on")).toHaveText([
      "driver being built",
    ]);
    await expect(pinned.locator(".impl-off")).toHaveCount(0);
    await expect(pinned.locator(".driver-line .micro")).toHaveText([
      "attempt 1",
    ]);
    // Still not openable -- there is no artefact to describe yet.
    await expect(pinned.locator(".driver-toggle")).toBeDisabled();
  });

  test("a build that stopped needs a human, and says why", async ({ page }) => {
    const types = withBuildAt(1, {
      state: "needs_human",
      open: false,
      attempts: 2,
      branch: "driver-loop/nabu-20261124t161000z",
      commit: "",
      error: "pytest failed: tests/test_nabu_driver.py::test_cost",
    });
    await mock(page, { types });
    await page.goto("/plan-types");

    const stopped = page.locator(".plan-type").nth(1).locator(".impl-bad");
    await expect(stopped).toHaveText(["driver build stopped"]);
    await expect(stopped).toHaveAttribute(
      "title",
      /pytest failed: tests\/test_nabu_driver\.py/,
    );
    await expect(
      page.locator(".plan-type").nth(1).locator(".driver-line .micro"),
    ).toHaveText(["2 attempts"]);
  });

  test("a stopped build can be retired, and the vendor unblocks", async ({
    page,
  }) => {
    const stopped = withBuildAt(1, {
      id: "drv_nabu_stopped",
      state: "needs_human",
      open: false,
      attempts: 2,
      branch: "driver-loop/nabu-20261124t161000z",
      commit: "",
      error: "pytest failed: tests/test_nabu_driver.py::test_cost",
    });
    await mock(page, { types: stopped });

    // The read after a retire is the server's answer, not the page editing its
    // own row: the fixture answers null the second time, as the store would
    // once the build is retired.
    let retired = false;
    await page.route("**/ui/plan-types.json", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(retired ? withBuildAt(1, null) : stopped),
      }),
    );

    const posted = [];
    await page.route("**/v1/driver-jobs/*/retire", async (route) => {
      posted.push(route.request().url());
      retired = true;
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          id: "drv_nabu_stopped",
          vendor: "Nabu",
          state: "needs_human",
          retired_at: "2026-11-26T15:00:00Z",
        }),
      });
    });

    await page.goto("/plan-types");
    const row = page.locator(".plan-type").nth(1);
    await expect(row.locator('[data-verb="retire"]')).toHaveText([
      "Retire this build",
    ]);

    await row.locator('[data-verb="retire"]').click();

    await expect.poll(() => posted.length).toBe(1);
    expect(posted[0]).toContain("/v1/driver-jobs/drv_nabu_stopped/retire");
    // The row goes back to "nobody has written one", which is a different
    // sentence from the stop it was showing, and offers no way out again.
    await expect(row.locator(".impl-off")).toHaveText(["no driver installed"]);
    await expect(row.locator('[data-verb="retire"]')).toHaveCount(0);
  });

  test("a refused retire shows the server's sentence", async ({ page }) => {
    const stopped = withBuildAt(1, {
      id: "drv_nabu_stopped",
      state: "needs_human",
      open: false,
      attempts: 2,
      branch: "",
      commit: "",
      error: "pytest failed",
    });
    await mock(page, { types: stopped });
    await page.route("**/v1/driver-jobs/*/retire", (route) =>
      route.fulfill({
        status: 409,
        contentType: "application/json",
        body: JSON.stringify({
          error: {
            code: "driver_retire_refused",
            message:
              "only a driver build that stopped for a human can be retired: this one is still queued or being worked",
            reason: "not_stopped",
          },
        }),
      }),
    );

    await page.goto("/plan-types");
    const row = page.locator(".plan-type").nth(1);
    await row.locator('[data-verb="retire"]').click();
    await expect(row.locator(".retire-failure")).toHaveText([
      "only a driver build that stopped for a human can be retired: this one is still queued or being worked",
    ]);
    // The row is still there to retry: a refusal is not a retire.
    await expect(row.locator('[data-verb="retire"]')).toBeEnabled();
  });

  test("an installed driver says which build produced it", async ({ page }) => {
    await mock(page);
    await page.goto("/plan-types");

    // The provenance is the one thing in the panel that has an origin: a
    // driver was written, and the branch and commit are where to read it.
    const detail = page.locator(".plan-type").first().locator(".driver-detail");
    await expect(detail).toContainText("build");
    await expect(detail).toContainText("installed");
    await expect(detail).toContainText("attempt 1");
    await expect(detail).toContainText(
      "driver-loop/databento-20261124t160220z",
    );
    await expect(detail).toContainText("9f28f75");

    // The transcript is the other half of that provenance: the branch says what
    // was written, the conversation says who wrote it and what it cost. Linked
    // only because this deployment names its canvas.
    // By name: the detail also links to the code, so `a.ref` alone now
    // matches two links.
    const transcript = detail
      .locator("a.ref")
      .filter({ hasText: "transcript" });
    await expect(transcript).toHaveText("transcript");
    await expect(transcript).toHaveAttribute(
      "href",
      "http://canvas.local:3088/canvas/conversations/a31b725e-c0ee-4e27-abfa-38dc921e83c8",
    );
  });

  test("a deep link opens the type and the driver it names", async ({
    page,
  }) => {
    // The dashboard links straight to /plan-types#<type>.
    await mock(page);
    await page.goto("/plan-types#market_data_window/databento");
    await expect(page.locator("#market_data_window h2")).toHaveText(
      "market_data_window",
    );
    await expect(
      page.locator("#market_data_window").locator(".driver-toggle"),
    ).toHaveAttribute("aria-expanded", "true");

    // A type that is not the first one is still just a section on the page.
    await page.goto("/plan-types#http_fetch");
    await expect(page.locator("#http_fetch h2")).toHaveText("http_fetch");
  });

  // An installed artefact no plan type asks for is a state an operator needs
  // to see. It belongs after the list, because it is not a question.
  test("drivers no plan type accepts are listed after the types", async ({
    page,
  }) => {
    await mock(page);
    await page.goto("/plan-types");

    const last = page.locator(".plan-type").last();
    await expect(last).toHaveClass(/orphans/);
    await expect(last.locator("h2")).toHaveText("drivers with no plan type");
    await expect(last.locator(".driver-name")).toHaveText(["stub"]);
    await expect(last.locator(".vendor")).toHaveText(["Stub"]);
    // And it is not inside any plan type's driver list.
    await expect(page.locator("#market_data_window .driver-name")).toHaveCount(
      0,
    );
  });

  test("a runtime with nothing unmatched grows no extra section", async ({
    page,
  }) => {
    // The page must not reserve space for a case that does not apply: with one
    // installed driver and it answering a type, the trailing section is absent
    // entirely rather than rendered empty.
    await mock(page, {
      types: {
        ...planTypes,
        drivers: {
          ...planTypes.drivers,
          count: 1,
          installed: planTypes.drivers.installed.filter(
            (row) => row.answers.length > 0,
          ),
        },
      },
    });
    await page.goto("/plan-types");

    await expect(page.locator(".plan-type")).toHaveCount(2);
    await expect(page.locator(".orphans")).toHaveCount(0);
    expect(await page.locator("body").innerText()).not.toContain(
      "drivers with no plan type",
    );
  });

  // The Svelte port kept the header count and dropped the per-field marker, so
  // the page said *how many* parameters were required but not *which*. The
  // count is not a substitute for the tag, so both are asserted -- inside the
  // disclosure that now holds the table.
  async function openFields(page, section) {
    await section.locator(".asks > summary").click();
    await expect(section.locator(".tbl-row").first()).toBeVisible();
  }

  test("each parameter says whether it is required", async ({ page }) => {
    await mock(page);
    await page.goto("/plan-types");
    const first = page.locator(".plan-type").first();
    await openFields(page, first);

    // The label carries the count, so the disclosure is useful folded too.
    await expect(first.locator(".asks > summary")).toContainText(
      "2 required, 1 optional",
    );

    const required = first.locator(".tbl-row").filter({ hasText: "dataset" });
    // Visible, not merely present: the marker sits inline after the name
    // because DataTable drops the header row under 600px, and a hidden tag
    // would be the same regression in a quieter form.
    await expect(required.locator(".req")).toBeVisible();
    await expect(required.locator(".req")).toHaveText("required");
    await expect(
      first.locator(".tbl-row").filter({ hasText: "symbols" }).locator(".req"),
    ).toHaveCount(0);

    // The enum is spelled out, not hidden behind a schema reference.
    await expect(first).toContainText("enum: GLBX.MDP3, XNAS.ITCH");
    // Property order stays the YAML's fill-in order.
    await expect(first.locator(".tbl-row .name code")).toHaveText([
      "dataset",
      "start",
      "symbols",
    ]);
    // A field with no description is an em dash, not an empty cell.
    await expect(first.locator(".tbl-row").nth(2).locator(".desc")).toHaveText(
      "—",
    );
  });

  test("the marker survives a phone, where the header row does not", async ({
    page,
  }) => {
    await mock(page);
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto("/plan-types");
    const first = page.locator(".plan-type").first();
    await openFields(page, first);

    // The reason the marker is inline and not a fifth column.
    await expect(first.locator(".tbl-head")).toBeHidden();

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

  test("the page is the drivers and the schema, and no glossary", async ({
    page,
  }) => {
    // A definition list used to sit under each table: extra fields, verify,
    // resume, source. Four labels of prose about a schema, on a page whose job
    // is to answer "which driver takes this, and which fields does it need?".
    await mock(page);
    await page.goto("/plan-types");

    await expect(page.locator(".kv")).toHaveCount(0);
    const body = await page.locator("body").innerText();
    // Labels of the removed glossary. "verify" is deliberately not in this
    // list: the type's description says the download is verified against the
    // vendor's manifest, which is the one place the word belongs.
    for (const gone of [
      "extra fields",
      "resume",
      "vendor-agnostic",
      "answered by",
      "what it asks",
      "seller",
    ]) {
      expect(body.toLowerCase()).not.toContain(gone.toLowerCase());
    }
    // The intro is a label, not an essay: no second paragraph above the list.
    expect((await page.locator(".lede").innerText()).length).toBeLessThan(20);
    await expect(page.locator(".sub-lede")).toHaveCount(0);

    // The raw schema still declares its dialect, so a copy-paste has the rules.
    const first = page.locator(".plan-type").first();
    await first.locator(".asks > summary").click();
    await first.locator(".raw > summary").click();
    await expect(first.locator(".raw pre")).toContainText("draft/2020-12");
  });

  test("nothing on the page can change the registry", async ({ page }) => {
    await mock(page);
    await page.goto("/plan-types");
    // The registry is git, not the database: a control here would be a lie.
    // The driver rows are disclosures; none of them writes.
    await expect(page.locator("form")).toHaveCount(0);
    await expect(page.locator("input")).toHaveCount(0);
    // The other page is one click away, and the token rides along or it 401s.
    await expect(page.locator(".main-nav a").first()).toHaveText("Requests");
  });

  test("an empty registry says every submission will be rejected", async ({
    page,
  }) => {
    await mock(page, {
      types: { types: [], drivers: planTypes.drivers },
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
    await expect(page.locator(".plan-type")).toHaveCount(3);
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
    await expect(page.locator(".plan-type")).toHaveCount(3);
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
      "market_data_window",
    );
    await expect(first.locator(".req-id")).toHaveCount(0);
    await expect(first).not.toContainText("9f2a41c8");
    // Who is being asked, and what the code means. A dataset is vendor
    // vocabulary, so the second line carries the vendor's own name for it --
    // `GLBX.MDP3` on its own is a name only Databento uses.
    await expect(first.locator(".req-vendor")).toHaveText(
      "Databento · GLBX.MDP3 · CME Globex MDP 3.0",
    );

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

    // The append-only trail, which no list view has room for. It reads as
    // sentences now: the stored name (`submitted`) is on the line's `title`,
    // because a page that prints machine names is a page that says nothing.
    await expect(page.locator(".events li")).toHaveCount(1);
    await expect(page.locator(".events li")).toContainText(
      "filed as an intent",
    );

    // The `filed` date, and no age beside it: an age is a clock the reader has
    // to subtract from, which is the same fact said twice.
    const labels = await page
      .locator(".detail-card .meta-cell dt")
      .allTextContents();
    expect(labels).toContain("filed");
    expect(labels).not.toContain("age");

    // The body the caller sent, verbatim, behind a disclosure.
    await page.locator("details.raw summary").click();
    await expect(page.locator("details.raw pre")).toContainText("OPRA.PILLAR");
  });

  test("the evidence leads, and the verbs follow it", async ({ page }) => {
    await mock(page, { detail: draftDetail });
    await page.goto("/requests/req_draft01");

    // The order is the argument, and it reversed. Approve used to lead the
    // page on the argument that a reader opens it to *do* something -- which
    // asked for the decision above the parameters and the sentence saying why
    // the request exists. A request page is a decision record: what was asked
    // for, why, and then the buttons.
    const order = await page
      .locator(".detail-card .block")
      .evaluateAll((blocks) =>
        blocks.map((block) =>
          block.querySelector(".micro")?.textContent.trim(),
        ),
      );
    // One cluster now, not a card and then a row of loose buttons: the heading
    // is "actions" whatever the row offers, and every verb lives under it.
    // `if you approve` sits with the evidence, not with the verbs: it says what
    // the button will do, which a reader needs *before* they reach the button.
    expect(order.filter(Boolean).slice(0, 4)).toEqual([
      "what was asked for",
      "why",
      "if you approve",
      "actions",
    ]);

    // Position, not just order in a list: the buttons are *below* the request
    // on the page, so a reader who scrolls to them has already passed the
    // question they are answering.
    const requestBox = await page.locator(".block.request").boundingBox();
    const approveBox = await page
      .locator('.verbs [data-verb="approve"]')
      .boundingBox();
    const cancelBox = await page
      .locator('.verbs [data-verb="cancel"]')
      .boundingBox();
    expect(approveBox.y).toBeGreaterThan(requestBox.y);
    expect(cancelBox.y).toBeGreaterThan(requestBox.y);

    const request = page.locator(".block.request");
    // A request as a person says it: the list is a list and the window is a
    // range, not two JSON blobs shown one key at a time.
    // The dataset param, named. The code is drawn the same way here as on the
    // book and the datasets page: one helper, three surfaces.
    await expect(request).toContainText("GLBX.MDP3 · CME Globex MDP 3.0");
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
    await expect(request.locator(".spec")).toContainText("$0.81");
    await expect(request.locator(".spec")).toContainText("$5");
  });

  test("the page says who filed it, and what approving would run", async ({
    page,
  }) => {
    await mock(page, { detail: draftDetail });
    await page.goto("/requests/req_draft01");

    // Provenance: the row is a declaration of *what* filed it, and the page
    // repeats it beside the other facts about the row.
    const filed = page.locator(".detail-card .meta-cell", {
      hasText: "filed by",
    });
    await expect(filed).toHaveCount(1);
    await expect(filed.locator("dd")).toHaveText("page");

    // The prospect is read off the sealed Job, so the steps are the runner's
    // steps in the runner's order -- not a marketing summary of them.
    const prospect = page.locator(".block.prospect");
    await expect(prospect.locator(".micro")).toHaveText("if you approve");
    const steps = await prospect.locator(".steps .verb").allTextContents();
    expect(steps).toEqual([
      "ask the vendor what it has, and what this window costs",
      "price it against the vendor's own metadata",
      "hand the vendor a batch job and keep its id",
      "wait for the vendor to finish",
      "fetch the files",
      "convert the landing files",
      "check the vendor's manifest against the bytes",
    ]);
    await expect(prospect).toContainText(
      "/srv/data/datasets/GLBX.MDP3/req_draft01/",
    );
    // Checks are recorded facts, and each carries a verdict rather than a
    // promise, so a reader can tell a guarantee from an expectation.
    await expect(prospect.locator(".checks dt")).toHaveCount(3);
    await expect(prospect).toContainText("DATA_DOWNLOAD_APPROVERS");
  });

  test("a row with no Job says approving it would run nothing", async ({
    page,
  }) => {
    // A blocked intent. "What will happen if I approve" has no honest answer
    // when there is nothing to run, and guessing one would be worse than
    // saying so.
    await mock(page, { detail: requestDetail });
    await page.goto(`/requests/${requestDetail.id}`);

    const prospect = page.locator(".block.prospect");
    await expect(prospect).toContainText("no Job");
    await expect(prospect).toContainText("awaiting_plan");
    await expect(prospect.locator(".steps li")).toHaveCount(0);
  });

  test("a quote under a cent is cheap, not free", async ({ page }) => {
    // A real one-day, one-symbol Databento quote: $0.0000099. Rounded to two
    // decimals that is `$0.00`, which is the claim "no charge" -- and the
    // difference between "cheap" and "free" is the whole question this figure
    // answers before somebody approves a spend.
    await mock(page, {
      detail: { ...draftDetail, estimate_usd: 9.909272e-6 },
    });
    await page.goto("/requests/req_draft01");

    await expect(page.locator(".block.request .kv")).toContainText("<$0.01");
    await expect(page.locator(".block.request .kv")).not.toContainText("$0.00");
    await expect(page.locator(".block.actions .ask")).toContainText(
      "Quoted <$0.01.",
    );
  });

  test("a window says which end is exclusive", async ({ page }) => {
    await mock(page, { detail: draftDetail });
    await page.goto("/requests/req_draft01");
    // The arrow alone cannot say whether the second date is included, and half
    // of the readers guess wrong -- expensively, since a day is a day.
    await expect(page.locator(".block.request .kv")).toContainText(
      "2026-11-04 → 2026-11-06 (end exclusive)",
    );
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
    // `submitted` followed by `submitter` was the same word twice, and the
    // page now says what happened rather than repeating the store's name.
    await expect(rows.nth(0).locator(".what")).toHaveText("filed as a plan");
    await expect(rows.nth(0).locator(".who")).toHaveCount(0);
    // An approval is worth a name, and the name is the login the tailnet
    // asserted -- not the word "human".
    await expect(rows.nth(1).locator(".who")).toHaveText("nick@example.com");
  });

  // The complaint this answers: a request that waited on a driver printed the
  // word "driver_state" once a minute, so the trail read as noise and the
  // moment something actually moved was invisible.
  test("a run of identical events is one line, and a change is its own", async ({
    page,
  }) => {
    const stop = (id, ts) => ({
      id,
      ts,
      actor_kind: "system",
      event: "driver_state",
      summary: "driver needs_human (Databento) — the agent never pushed",
      detail: { driver_state: "needs_human", vendor: "Databento" },
    });
    await mock(page, {
      detail: {
        ...draftDetail,
        events: [
          draftDetail.events[0],
          stop(2, "2026-11-25T19:17:58Z"),
          stop(3, "2026-11-25T19:18:58Z"),
          stop(4, "2026-11-25T19:19:58Z"),
          {
            ...stop(5, "2026-11-25T19:20:58Z"),
            summary: "driver needs_human (Databento) — a bill",
          },
        ],
      },
    });
    await page.goto("/requests/req_draft01");

    const rows = page.locator(".events li");
    // Three identical stops are one line; the different reason is not folded
    // into them, because a change of reason is the interesting part.
    await expect(rows).toHaveCount(3);
    await expect(rows.nth(0).locator(".what")).toHaveText("filed as a plan");
    await expect(rows.nth(1).locator(".what")).toHaveText(
      "driver needs_human (Databento) — the agent never pushed",
    );
    await expect(rows.nth(1).locator(".who")).toHaveText("×3");
    await expect(rows.nth(1).locator(".who")).toHaveAttribute(
      "title",
      "last at 2026-11-25T19:19:58Z",
    );
    await expect(rows.nth(2).locator(".what")).toContainText("a bill");
    await expect(rows.nth(2).locator(".who")).toHaveCount(0);
    // The store's own name is still reachable, for grepping.
    await expect(rows.nth(1)).toHaveAttribute("title", "driver_state");
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
  const approveButton = (page) => page.locator('.verbs [data-verb="approve"]');

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
    // The ceiling is in the line above the button, not in the button's own
    // word: the button says what the click does, and says it once.
    await expect(approveButton(page)).toHaveText("Approve");
    await expect(page.locator(".approve .ask")).toContainText("up to $5");
  });

  test("the numbers are read before the click, and one click is the spend", async ({
    page,
  }) => {
    const seen = await interceptApprove(page);
    await mock(page, { detail: draftDetail });
    await page.goto("/requests/req_draft01");

    // What is being authorised is on the page before anybody clicks: the quote
    // the vendor gave for this Job and the ceiling the approval authorises.
    // Two figures, no prose -- the sentence around them only got in the way.
    const ask = page.locator(".approve .ask");
    await expect(ask).toHaveText("Quoted $0.81. Approves up to $5.");
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
    await expect(ask).toHaveText(
      "Quoted $0.81. No ceiling — nothing here bounds the spend.",
    );
    await expect(approveButton(page)).toHaveText("Approve");
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

    await expect(page.locator('.failure[data-verb="approve"]')).toContainText(
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
      "Changing a request as someone-else@example.com is refused",
    );
    await expect(page.locator(".approve .who")).toContainText(
      "DATA_DOWNLOAD_APPROVERS",
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
    await expect(page.locator(".block.actions .failure")).toContainText(
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

  test("a row waiting for a driver says so, not 'no worker'", async ({
    page,
  }) => {
    // Phase 5.5: the loop has the row, so "no worker has picked it up" would
    // send the reader looking for a runner when the repair is a driver.
    await mock(page, {
      detail: {
        ...draftDetail,
        state: "approved",
        approved_unclaimed: true,
        driver_state: "building",
      },
    });
    await page.goto("/requests/req_draft01");
    const grid = page.locator(".meta-grid");
    await expect(grid).toContainText("driver");
    await expect(grid).toContainText(
      "none installed for this vendor — the loop is building one",
    );
    await expect(grid).not.toContainText("no worker has picked it up");
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
      "market_data_window",
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
    // The heading is where the code is drawn, so it is where the vendor's name
    // for it belongs -- and how far back it goes is the fact a code is most
    // often asked about.
    await expect(page.locator(".vendor h3")).toHaveText(
      "GLBX.MDP3 · CME Globex MDP 3.0",
    );
    await expect(page.locator(".since")).toHaveText(
      "available 2010-06-06 → now",
    );
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

test.describe("a failure says why", () => {
  // The runner has always written a reason and no page read it: a `failed`
  // row said *Failed* and stopped there, so the one question the reader came
  // with -- "no driver? no key? the vendor refused?" -- needed a shell.
  test("the book puts the reason under the badge", async ({ page }) => {
    await mock(page);
    await page.goto("/");

    const row = page.locator(".tbl-row").filter({ hasText: "#5" });
    await expect(row.locator(".pill")).toHaveText("Landed with errors");
    await expect(row.locator(".state .detail")).toHaveText("verify shortfall");
    await expect(row.locator(".state .detail")).toHaveAttribute(
      "title",
      "the vendor sent 3 of the 5 files the manifest claimed",
    );
  });

  test("the page says why, before it says what was asked for", async ({
    page,
  }) => {
    await mock(page, {
      detail: {
        ...draftDetail,
        state: "failed",
        facet: "done",
        facet_label: "Finished",
        failure: {
          code: "driver_not_ready",
          detail:
            "no driver is installed for vendor 'Databento' (installed: stub v1 (Stub))",
        },
        identity: {
          ...draftDetail.identity,
          errors: {
            error: "driver_not_ready",
            detail:
              "no driver is installed for vendor 'Databento' (installed: stub v1 (Stub))",
          },
        },
      },
    });
    await page.goto("/requests/req_draft01");

    // The phrase the code owns, verbatim, plus the runner's sentence under it.
    await expect(page.locator(".block.failed .failed-code")).toHaveText(
      "driver_not_ready",
    );
    await expect(page.locator(".block.failed .failed-detail")).toHaveText(
      "no driver is installed for vendor 'Databento' (installed: stub v1 (Stub))",
    );

    // And it leads the page: a reader who opens a failed row is asking why
    // before anything else, and the fix (Retry) now sits below the evidence
    // like every other verb.
    const order = await page
      .locator(".detail-card .block")
      .evaluateAll((blocks) =>
        blocks.map((block) =>
          block.querySelector(".micro")?.textContent.trim(),
        ),
      );
    const blocks = await page
      .locator(".detail-card .block")
      .evaluateAll((all) => all.map((block) => block.className));
    expect(blocks[0]).toContain("failed");
    expect(order.filter(Boolean).slice(0, 3)).toEqual([
      "why it failed",
      "what was asked for",
      "why",
    ]);
    const requestBox = await page.locator(".block.request").boundingBox();
    const retryBox = await page
      .locator('.verbs [data-verb="retry"]')
      .boundingBox();
    expect(retryBox.y).toBeGreaterThan(requestBox.y);
  });

  test("a row that did not fail grows no empty heading", async ({ page }) => {
    await mock(page, { detail: draftDetail });
    await page.goto("/requests/req_draft01");
    await expect(page.locator(".block.failed")).toHaveCount(0);
  });
});

test.describe("a retry files the same request again", () => {
  // The failure this exists for is `driver_not_ready`: the repair happens in
  // another container, and the row that failed cannot be approved or run —
  // it is history. So the page offers a *new* request, and says so.
  const failed = (failure) => ({
    ...draftDetail,
    state: "failed",
    facet: "done",
    facet_label: "Finished",
    failure,
  });

  const DRIVERLESS = {
    code: "driver_not_ready",
    detail:
      "no driver is installed for vendor 'Databento' (installed: stub v1 (Stub))",
  };

  test("offers the verb, and files a new request rather than rewinding", async ({
    page,
  }) => {
    let posted = null;
    await page.route("**/v1/requests/*/retry", async (route) => {
      posted = route.request().url();
      await route.fulfill({
        status: 202,
        contentType: "application/json",
        body: JSON.stringify({
          request_id: "req_9f2c41ab",
          state: "draft",
          retried_from: "req_draft01",
          quote_usd: 1.466523885727,
        }),
      });
    });
    await mock(page, { detail: failed(DRIVERLESS) });
    await page.goto("/requests/req_draft01");

    const actions = page.locator(".block.actions");
    // The verb is a button and nothing else: retry is self-explanatory, and a
    // paragraph above it was text the reader had to get past to reach the verb.
    await expect(page.locator('.verbs [data-verb="retry"]')).toBeVisible();
    await expect(page.locator(".verbs p")).toHaveCount(0);
    await page.getByRole("button", { name: "Retry" }).click();

    // The answer is the one thing it does say: what was filed, for how much.
    await expect(actions).toContainText("req_9f2c41ab");
    await expect(actions).toContainText("$1.47");
    await expect(actions).toContainText("waiting for approval");
    expect(posted).toContain("/v1/requests/req_draft01/retry");
    // The reason block is still there: the old row is unchanged, and the page
    // is still its page.
    await expect(page.locator(".block.failed")).toContainText(
      "driver_not_ready",
    );
  });

  test("is not offered where the same bytes would fail again", async ({
    page,
  }) => {
    // A verifier's verdict is a property of the vendor's files and the
    // manifest. Both are immutable, so a retry is the same money twice.
    await mock(page, {
      detail: failed({ code: "verify_shortfall", detail: "3 of 5 files" }),
    });
    await page.goto("/requests/req_draft01");
    await expect(page.locator(".block.failed")).toBeVisible();
    await expect(page.locator('.verbs [data-verb="retry"]')).toHaveCount(0);
  });

  test("is not offered on a row that has not settled", async ({ page }) => {
    await mock(page, { detail: draftDetail });
    await page.goto("/requests/req_draft01");
    await expect(page.locator('.verbs [data-verb="retry"]')).toHaveCount(0);
  });

  test("a login that may read but not file is told so, and gets no button", async ({
    page,
  }) => {
    await mock(page, {
      detail: failed(DRIVERLESS),
      who: { login: "guest@example.com", approver: false },
    });
    await page.goto("/requests/req_draft01");
    const actions = page.locator(".block.actions");
    await expect(actions).toContainText("DATA_DOWNLOAD_APPROVERS");
    await expect(actions.getByRole("button")).toHaveCount(0);
  });

  test("the server's own refusal is what the page shows", async ({ page }) => {
    await page.route("**/v1/requests/*/retry", (route) =>
      route.fulfill({
        status: 409,
        contentType: "application/json",
        body: JSON.stringify({
          error: {
            code: "not_retryable",
            message:
              "this row failed a check the same bytes would fail again " +
              "(the vendor's files and the manifest are both immutable)",
          },
        }),
      }),
    );
    await mock(page, { detail: failed(DRIVERLESS) });
    await page.goto("/requests/req_draft01");
    await page.getByRole("button", { name: "Retry" }).click();
    await expect(page.locator('.failure[data-verb="retry"]')).toContainText(
      "immutable",
    );
  });
});

test.describe("a drafted request can be cancelled", () => {
  // The case: a draft nobody wants — filed twice, or filed by mistake. Until
  // this card the only ways out were approving it (a spend) or editing the
  // store (a lie about the record). Cancelling keeps the row: it is a
  // statement about a filing, not a deletion.
  const cancelled = {
    request_id: "req_draft01",
    state: "cancelled",
    cancelled: true,
  };

  test("asks twice, then cancels, and says the row stays", async ({ page }) => {
    let posted = null;
    await page.route("**/v1/requests/*/cancel", async (route) => {
      posted = route.request().url();
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(cancelled),
      });
    });
    await mock(page, { detail: draftDetail });
    await page.goto("/requests/req_draft01");

    const actions = page.locator(".block.actions");
    // No paragraph beside the verb: the two-click confirm is the explanation.
    await expect(page.locator(".verbs p")).toHaveCount(0);
    await expect(page.locator('.verbs [data-verb="cancel"]')).toBeVisible();

    // The first click only asks. Nothing has been posted.
    await page.getByRole("button", { name: "Cancel" }).click();
    expect(posted).toBeNull();
    await expect(actions).toContainText("Yes, cancel");

    await page.getByRole("button", { name: "Yes, cancel" }).click();
    await expect(actions).toContainText("Cancelled.");
    expect(posted).toContain("/v1/requests/req_draft01/cancel");
  });

  test("the first click can be taken back", async ({ page }) => {
    let posted = null;
    await page.route("**/v1/requests/*/cancel", async (route) => {
      posted = route.request().url();
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(cancelled),
      });
    });
    await mock(page, { detail: draftDetail });
    await page.goto("/requests/req_draft01");

    await page.getByRole("button", { name: "Cancel" }).click();
    await page.getByRole("button", { name: "Keep it" }).click();

    await expect(page.getByRole("button", { name: "Cancel" })).toBeVisible();
    expect(posted).toBeNull();
  });

  test("is not offered once the request has been approved", async ({
    page,
  }) => {
    // Approving authorises exactly one attempt; taking it back is not a
    // book-keeping change, and the server refuses it as well.
    await mock(page, {
      detail: { ...draftDetail, state: "approved", facet: "todo" },
    });
    await page.goto("/requests/req_draft01");
    await expect(page.locator('.verbs [data-verb="cancel"]')).toHaveCount(0);
  });

  test("is not offered on a row that has already run", async ({ page }) => {
    await mock(page, {
      detail: {
        ...draftDetail,
        state: "failed",
        facet: "done",
        failure: { code: "driver_not_ready", detail: "no driver" },
      },
    });
    await page.goto("/requests/req_draft01");
    await expect(page.locator('.verbs [data-verb="cancel"]')).toHaveCount(0);
  });

  test("a login that may read but not file is told so, and gets no button", async ({
    page,
  }) => {
    await mock(page, {
      detail: draftDetail,
      who: { login: "someone-else@example.com", approver: false },
    });
    await page.goto("/requests/req_draft01");
    const actions = page.locator(".block.actions");
    await expect(actions).toContainText("DATA_DOWNLOAD_APPROVERS");
    await expect(actions.getByRole("button")).toHaveCount(0);
  });

  test("the server's own refusal is what the page shows", async ({ page }) => {
    await page.route("**/v1/requests/*/cancel", (route) =>
      route.fulfill({
        status: 409,
        contentType: "application/json",
        body: JSON.stringify({
          error: {
            code: "cancel_refused",
            message:
              "only a request waiting for approval can be cancelled: this " +
              "one has been authorised, has run, or was never bound to a plan",
          },
        }),
      }),
    );
    await mock(page, { detail: draftDetail });
    await page.goto("/requests/req_draft01");
    await page.getByRole("button", { name: "Cancel" }).click();
    await page.getByRole("button", { name: "Yes, cancel" }).click();
    await expect(page.locator('.failure[data-verb="cancel"]')).toContainText(
      "has been authorised",
    );
  });
});

test.describe("a cancelled request is kept, not listed", () => {
  // The list answers "what needs me". A cancelled draft needs nothing, so the
  // server takes it out of `rows` and out of `total` — a count that disagrees
  // with what is drawn is the bug this is pinning. It is not gone: it is in the
  // `cancelled` facet, offered by name in the filter bar.
  const withCancelled = (() => {
    const row = {
      ...dashboard.rows[0],
      seq: 9,
      id: "req_gone0001",
      state: "cancelled",
      facet: "cancelled",
      facet_label: "Cancelled",
      failure: null,
      stuck_condition: null,
      stuck_detail: null,
      approved_unclaimed: false,
    };
    // The server's own output, not the store: `rows` is already the list minus
    // what needs nothing, and the cancelled row lives in its facet alone.
    const facets = { ...dashboard.facets, cancelled: [row] };
    return { ...dashboard, facets, cancelled: 1 };
  })();

  test("is out of the default list, and the count agrees", async ({ page }) => {
    await mock(page, { requests: withCancelled });
    await page.goto("/");

    await expect(page.locator(".tbl-row")).toHaveCount(dashboard.rows.length);
    await expect(page.locator("body")).not.toContainText("#9");
    // Five drawn, five claimed.
    await expect(page.locator(".facet-option").first()).toContainText("5");
  });

  test("is reachable by name, with its own badge", async ({ page }) => {
    await mock(page, { requests: withCancelled });
    await page.goto("/");

    const option = page
      .locator(".facet-option")
      .filter({ hasText: "Cancelled" })
      .first();
    await expect(option).toBeVisible();
    await expect(option).toContainText("1");

    await option.click();
    const row = page.locator(".tbl-row").filter({ hasText: "#9" });
    await expect(row).toBeVisible();
    await expect(row).toContainText("Cancelled");
  });

  test("and a book with none does not offer the filter at all", async ({
    page,
  }) => {
    await mock(page, { requests: dashboard });
    await page.goto("/");
    await expect(
      page.locator(".facet-option").filter({ hasText: "Cancelled" }),
    ).toHaveCount(0);
  });
});

test.describe("a finished request can be archived", () => {
  // A failed row had one verb, retry, and no way to stop looking at it.
  // Cancel writes `state` and would rewrite the outcome; archive writes a
  // date, so the row keeps its badge and leaves the list.
  const failed = {
    ...draftDetail,
    state: "failed",
    facet: "done",
    facet_label: "Finished",
    failure: {
      code: "driver_not_ready",
      detail: "no driver is installed for vendor 'Databento'",
    },
    archived_at: null,
  };

  test("offers the verb on a failed row, and leaves the page with the row", async ({
    page,
  }) => {
    let posted = null;
    await page.route("**/v1/requests/*/archive", async (route) => {
      posted = route.request().postDataJSON();
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          request_id: "req_draft01",
          state: "failed",
          archived_at: "2026-11-23T01:30:00Z",
          changed: true,
        }),
      });
    });
    await mock(page, { detail: failed });
    await page.goto("/requests/req_draft01");

    await expect(page.locator('.verbs [data-verb="archive"]')).toBeVisible();
    await expect(page.locator(".verbs p")).toHaveCount(0);
    // The row is alphabetical and fixed: Archive before Retry, whatever else
    // the row offers. Two words, no explanation -- the glyph and the fill are
    // what make them findable.
    await expect(page.locator(".verbs").getByRole("button")).toHaveText([
      "Archive",
      "Retry",
    ]);
    // The badge is untouched by the verb that hides it.
    await expect(page.locator(".block.failed")).toContainText(
      "driver_not_ready",
    );

    await page.getByRole("button", { name: "Archive" }).click();
    expect(posted).toEqual({ archived: true });
    // Putting the row away ends the visit: the request page is a decision
    // record, and a decision you have finished with is not something to keep
    // reading. You land back on the list you archived it from, where the
    // Archived filter now has a row in it.
    await expect(page).toHaveURL(/\/$/);
    await expect(page.locator(".tbl-row").first()).toBeVisible();
  });

  test("an archived row offers the way back, not the way out", async ({
    page,
  }) => {
    let posted = null;
    await page.route("**/v1/requests/*/archive", async (route) => {
      posted = route.request().postDataJSON();
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          request_id: "req_draft01",
          state: "failed",
          archived_at: null,
          changed: true,
        }),
      });
    });
    await mock(page, {
      detail: {
        ...failed,
        facet: "archived",
        facet_label: "Archived",
        archived_at: "2026-11-23T01:00:00Z",
      },
    });
    await page.goto("/requests/req_draft01");

    await expect(page.locator('.verbs [data-verb="archive"]')).toHaveText(
      "Restore",
    );
    await page.getByRole("button", { name: "Restore" }).click();
    expect(posted).toEqual({ archived: false });
    // Asking for the row back keeps you on it: you are reading the thing you
    // just asked to keep, so only archiving ends the visit.
    await expect(page).toHaveURL(/\/requests\/req_draft01$/);
  });

  test("is not offered on a draft, which is cancelled instead", async ({
    page,
  }) => {
    await mock(page, { detail: draftDetail });
    await page.goto("/requests/req_draft01");
    await expect(page.locator('.verbs [data-verb="archive"]')).toHaveCount(0);
  });

  test("a login that may read but not file is told so, and gets no button", async ({
    page,
  }) => {
    await mock(page, {
      detail: failed,
      who: { login: "someone-else@example.com", approver: false },
    });
    await page.goto("/requests/req_draft01");
    const actions = page.locator(".block.actions");
    await expect(actions).toContainText("DATA_DOWNLOAD_APPROVERS");
    await expect(actions.getByRole("button")).toHaveCount(0);
  });
});

test.describe("an archived request is off the book", () => {
  const withArchived = (() => {
    const row = {
      // A row that really is finished with a reason: landed_with_errors, whose
      // badge says what went wrong and must keep saying it once archived.
      ...dashboard.rows.find((r) => r.state === "landed_with_errors"),
      seq: 8,
      id: "req_file0001",
      facet: "archived",
      facet_label: "Archived",
    };
    return {
      ...dashboard,
      facets: { ...dashboard.facets, archived: [row] },
      archived: 1,
    };
  })();

  test("the list leaves it out and offers the filter by name", async ({
    page,
  }) => {
    await mock(page, { requests: withArchived });
    await page.goto("/");

    await expect(page.locator(".tbl-row")).toHaveCount(dashboard.rows.length);
    await expect(page.locator("body")).not.toContainText("#8");

    const option = page
      .locator(".facet-option")
      .filter({ hasText: "Archived" })
      .first();
    await expect(option).toContainText("1");
    await option.click();

    const row = page.locator(".tbl-row").filter({ hasText: "#8" });
    await expect(row).toBeVisible();
    // Still says what it actually is -- the verifier's verdict -- and not
    // "archived": the filter is the list, the badge is the record.
    await expect(row).toContainText("verify shortfall");
  });
});
