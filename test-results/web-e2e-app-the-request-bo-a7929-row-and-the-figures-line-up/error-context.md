# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.
- Provide a snippet of code with the fix, if possible.

# Test info

- Name: web/e2e/app.spec.js >> the request book >> is one list with a badge per row, and the figures line up
- Location: web/e2e/app.spec.js:66:3

# Error details

```
Error: page.goto: Protocol error (Page.navigate): Cannot navigate to invalid URL
Call log:
  - navigating to "/", waiting until "load"

```

# Test source

```ts
  1   | import { expect, test } from "@playwright/test";
  2   | 
  3   | // Fixed clock rendering: the `filed` column is local time, and a test that
  4   | // asserted a string would otherwise pass in London and fail in Auckland.
  5   | test.use({ timezoneId: "UTC" });
  6   | import {
  7   |   blockedBook,
  8   |   dashboard,
  9   |   draftDetail,
  10  |   emptyBook,
  11  |   planTypes,
  12  |   requestDetail,
  13  | } from "./fixtures.js";
  14  | 
  15  | // Resource-type routing, not a URL glob: `**/stats` would also match the
  16  | // document navigation and hand the browser JSON instead of the app.
  17  | // What the server says the tailnet hop asserted. Overridable per test: the
  18  | // approve card's whole behaviour is a function of this answer.
  19  | const SERVED = { login: "nick@example.com", approver: true };
  20  | 
  21  | async function mock(
  22  |   page,
  23  |   { requests, types, detail, who, whoStatus = 200 } = {},
  24  | ) {
  25  |   await page.route(
  26  |     "**/v1/whoami",
  27  |     (route) =>
  28  |       route.fulfill({
  29  |         status: whoStatus,
  30  |         contentType: "application/json",
  31  |         body: JSON.stringify(
  32  |           whoStatus === 200
  33  |             ? (who ?? SERVED)
  34  |             : {
  35  |                 error: {
  36  |                   code: "not_attested",
  37  |                   message:
  38  |                     "no Tailscale-User-Login header: this request arrived " +
  39  |                     "from 192.168.1.40 directly, not through `tailscale serve`.",
  40  |                 },
  41  |               },
  42  |         ),
  43  |       }),
  44  |     { times: 99 },
  45  |   );
  46  |   await page.route(
  47  |     "**/ui/**",
  48  |     (route) => {
  49  |       const url = route.request().url();
  50  |       const body = url.includes("plan-types")
  51  |         ? (types ?? planTypes)
  52  |         : url.includes("/ui/request/")
  53  |           ? (detail ?? requestDetail)
  54  |           : (requests ?? dashboard);
  55  |       return route.fulfill({
  56  |         status: 200,
  57  |         contentType: "application/json",
  58  |         body: JSON.stringify(body),
  59  |       });
  60  |     },
  61  |     { times: 99 },
  62  |   );
  63  | }
  64  | 
  65  | test.describe("the request book", () => {
  66  |   test("is one list with a badge per row, and the figures line up", async ({
  67  |     page,
  68  |   }) => {
  69  |     await mock(page);
> 70  |     await page.goto("/");
      |                ^ Error: page.goto: Protocol error (Page.navigate): Cannot navigate to invalid URL
  71  | 
  72  |     await expect(page.locator(".tbl-row")).toHaveCount(5);
  73  |     // Four columns: what it is, where it is, what it costs, when it was asked
  74  |     // for. "why it matters" was the one removed -- on a finished row it either
  75  |     // repeated the badge or rendered a dash, and its useful half (what a
  76  |     // blocked row is short of) is a qualification of the state, so it sits
  77  |     // under the badge now.
  78  |     await expect(page.locator(".tbl-head .col-label")).toHaveText([
  79  |       "request",
  80  |       "state",
  81  |       "quote",
  82  |       "filed",
  83  |     ]);
  84  | 
  85  |     // The columns of a grid share one template, which is what makes the
  86  |     // numeric column line up rather than land wherever the previous cell ended.
  87  |     const template = await page
  88  |       .locator(".tbl-row")
  89  |       .first()
  90  |       .evaluate((el) => getComputedStyle(el).gridTemplateColumns);
  91  |     expect(template.split(" ").length).toBe(4);
  92  | 
  93  |     const first = page.locator(".tbl-row").first();
  94  |     // Phase 4: the quote is on the row, at the precision a purchase needs.
  95  |     await expect(first.locator(".quote")).toHaveText("$0.0412");
  96  |     // When it was filed, in a form a person reads. Not "1h 4m": an age answers
  97  |     // a question nobody asked and has to be converted back into a clock.
  98  |     await expect(first.locator(".filed")).toHaveText("14 Nov 23:10");
  99  |     await expect(first.locator(".pill")).toHaveText("Succeeded");
  100 |   });
  101 | 
  102 |   test("a stuck condition outranks the state it is stuck in", async ({
  103 |     page,
  104 |   }) => {
  105 |     await mock(page);
  106 |     await page.goto("/");
  107 | 
  108 |     // The second row filed: `running`, and stuck.
  109 |     const running = page.locator(".tbl-row").nth(1);
  110 |     // The badge says `running`; the line under it is the fact that makes the
  111 |     // row wrong. One pill, not two: a running row is not in a second state, it
  112 |     // is in the one state with a problem.
  113 |     await expect(running.locator(".pill")).toHaveCount(1);
  114 |     await expect(running.locator(".pill")).toHaveText("Running");
  115 |     await expect(running.locator(".state .detail")).toHaveText("lease_expired");
  116 |     // And it is coloured as a fault, which is the whole reason the detail line
  117 |     // carries a tone -- otherwise a stuck row looks like an ordinary one.
  118 |     await expect(running.locator(".state .detail")).toHaveClass(/\bbad\b/);
  119 |   });
  120 | 
  121 |   test("the facet is one choice from N, drawn as a histogram", async ({
  122 |     page,
  123 |   }) => {
  124 |     await mock(page);
  125 |     await page.goto("/");
  126 | 
  127 |     const group = page.locator(".facet-group").first();
  128 |     const radios = group.locator('input[type="radio"]');
  129 |     // All, plus the seven facets. `waiting` used to be one of them; it is now
  130 |     // three (needs params / no plan / requested), because "waiting on a plan"
  131 |     // named a state and hid an owner.
  132 |     await expect(radios).toHaveCount(8);
  133 |     // One name for the whole group, or the browser lets you pick two.
  134 |     expect(
  135 |       new Set(await radios.evaluateAll((all) => all.map((i) => i.name))).size,
  136 |     ).toBe(1);
  137 | 
  138 |     // The bar is the count, relative to the largest option in its own group:
  139 |     // 2 finished out of 2 is a full bar.
  140 |     const widthOf = (label) =>
  141 |       group
  142 |         .locator(".facet-option")
  143 |         .filter({ hasText: label })
  144 |         .locator(".option-bar")
  145 |         .evaluate((bar) => parseFloat(getComputedStyle(bar).width));
  146 |     const [all, done] = [await widthOf("All"), await widthOf("Finished")];
  147 |     // Two finished of five requests: the bar is the count, relative to the
  148 |     // largest option in its own group.
  149 |     expect(done / all).toBeCloseTo(2 / 5, 1);
  150 |   });
  151 | 
  152 |   test("choosing a facet narrows the list and lands in the URL", async ({
  153 |     page,
  154 |   }) => {
  155 |     await mock(page);
  156 |     await page.goto("/");
  157 | 
  158 |     await page.locator(".facet-option").filter({ hasText: "Approval" }).click();
  159 |     await expect(page).toHaveURL(/show=todo/);
  160 |     await expect(page.locator(".tbl-row")).toHaveCount(1);
  161 |     await expect(page.locator(".tbl-row")).toContainText("#3");
  162 | 
  163 |     // A filter that matches nothing says so, and offers the way back.
  164 |     await page.locator(".facet-option").filter({ hasText: "Running" }).click();
  165 |     await expect(page.locator(".tbl-row")).toHaveCount(0);
  166 |     await expect(page.locator(".empty-state")).toContainText(
  167 |       "No matches for active filters",
  168 |     );
  169 | 
  170 |     // Clicking the active option clears it -- a single-select facet with no way
```