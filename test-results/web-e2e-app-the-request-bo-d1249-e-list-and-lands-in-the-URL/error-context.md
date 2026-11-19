# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.
- Provide a snippet of code with the fix, if possible.

# Test info

- Name: web/e2e/app.spec.js >> the request book >> choosing a facet narrows the list and lands in the URL
- Location: web/e2e/app.spec.js:152:3

# Error details

```
Error: page.goto: Protocol error (Page.navigate): Cannot navigate to invalid URL
Call log:
  - navigating to "/", waiting until "load"

```

# Test source

```ts
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
  70  |     await page.goto("/");
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
> 156 |     await page.goto("/");
      |                ^ Error: page.goto: Protocol error (Page.navigate): Cannot navigate to invalid URL
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
  171 |     // back is a trap.
  172 |     await page.locator(".facet-option").filter({ hasText: "Running" }).click();
  173 |     await expect(page).not.toHaveURL(/show=/);
  174 |     await expect(page.locator(".tbl-row")).toHaveCount(5);
  175 |   });
  176 | 
  177 |   test("an approved row says it is approved, and that nothing has it", async ({
  178 |     page,
  179 |   }) => {
  180 |     // Two words for two different questions, and they must not look like rival
  181 |     // answers to one. The badge answers "where is this"; the line under it
  182 |     // answers "what is missing". It used to say the participle `unclaimed`,
  183 |     // which beside a badge reading `Approved` read as a contradiction -- the
  184 |     // reader's question was "so which is it?".
  185 |     await mock(page);
  186 |     await page.goto("/");
  187 | 
  188 |     const row = page.locator(".tbl-row").filter({ hasText: "#3" });
  189 |     await expect(row.locator(".pill")).toHaveText("Approved");
  190 |     await expect(row.locator(".state .detail")).toHaveText(
  191 |       "no worker has it yet",
  192 |     );
  193 |   });
  194 | 
  195 |   test("an unapproved row says what approving it would cost", async ({
  196 |     page,
  197 |   }) => {
  198 |     // This row used to say it twice: the pill read `draft` and the next column
  199 |     // read `needs approval`. One fact, two spellings -- and `draft` was the
  200 |     // wrong one, since a draft is unsubmitted and editable and this row is
  201 |     // neither. The badge now names what is being waited on; the line under it
  202 |     // carries the fact the badge cannot: what approving would authorise.
  203 |     const draft = {
  204 |       ...dashboard.rows[2],
  205 |       // A keyed `each` is what makes the table correct and what makes a copied
  206 |       // id a crash: the row needs its own.
  207 |       id: "req_draft06",
  208 |       seq: 6,
  209 |       state: "draft",
  210 |       max_usd: 5,
  211 |       approved_unclaimed: false,
  212 |     };
  213 |     await mock(page, {
  214 |       requests: {
  215 |         ...dashboard,
  216 |         rows: [...dashboard.rows, draft],
  217 |         total: dashboard.total + 1,
  218 |       },
  219 |     });
  220 |     await page.goto("/");
  221 | 
  222 |     const row = page.locator(".tbl-row").filter({ hasText: "#6" });
  223 |     await expect(row.locator(".pill")).toHaveCount(1);
  224 |     await expect(row.locator(".pill")).toHaveText("Awaiting approval");
  225 |     await expect(row.locator(".state .detail")).toHaveText("up to $5");
  226 |     await expect(page.locator("body")).not.toContainText("needs approval");
  227 |   });
  228 | 
  229 |   test("a filter nobody knows about falls back to everything", async ({
  230 |     page,
  231 |   }) => {
  232 |     await mock(page);
  233 |     await page.goto("/?show=nonsense");
  234 |     await expect(page.locator(".tbl-row")).toHaveCount(5);
  235 |     // and it is corrected in the URL, so a refresh does not re-break it
  236 |     await expect(page).not.toHaveURL(/show=/);
  237 |   });
  238 | 
  239 |   test("?show= from a link filters on load", async ({ page }) => {
  240 |     await mock(page);
  241 |     await page.goto("/?show=broken");
  242 |     await expect(page.locator(".tbl-row")).toHaveCount(1);
  243 |     await expect(page.locator(".tbl-row")).toContainText("#2");
  244 |   });
  245 | 
  246 |   test("a link between the two pages carries no credential", async ({
  247 |     page,
  248 |   }) => {
  249 |     await mock(page);
  250 |     await page.goto("/");
  251 |     // Both views are public, so a nav tab is just a tab. No token in an href
  252 |     // anywhere on the page: a credential in a URL is a credential in a log.
  253 |     const links = page.locator('a[href="/plan-types"]');
  254 |     expect(await links.count()).toBeGreaterThan(0);
  255 |     const hrefs = await page
  256 |       .locator("a[href]")
```