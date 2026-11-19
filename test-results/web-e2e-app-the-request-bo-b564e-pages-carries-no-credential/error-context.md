# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.
- Provide a snippet of code with the fix, if possible.

# Test info

- Name: web/e2e/app.spec.js >> the request book >> a link between the two pages carries no credential
- Location: web/e2e/app.spec.js:246:3

# Error details

```
Error: page.goto: Protocol error (Page.navigate): Cannot navigate to invalid URL
Call log:
  - navigating to "/", waiting until "load"

```

# Test source

```ts
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
> 250 |     await page.goto("/");
      |                ^ Error: page.goto: Protocol error (Page.navigate): Cannot navigate to invalid URL
  251 |     // Both views are public, so a nav tab is just a tab. No token in an href
  252 |     // anywhere on the page: a credential in a URL is a credential in a log.
  253 |     const links = page.locator('a[href="/plan-types"]');
  254 |     expect(await links.count()).toBeGreaterThan(0);
  255 |     const hrefs = await page
  256 |       .locator("a[href]")
  257 |       .evaluateAll((els) => els.map((el) => el.getAttribute("href")));
  258 |     expect(hrefs.filter((href) => href.includes("token="))).toEqual([]);
  259 |     await expect(page).not.toHaveURL(/token=/);
  260 |   });
  261 | 
  262 |   test("the head is the home-screen entry, not a bare tab", async ({
  263 |     page,
  264 |   }) => {
  265 |     await mock(page);
  266 |     await page.goto("/");
  267 |     // These are the pre-existing phone bookmarks: the URLs are baked into home
  268 |     // screens, so the markup that names them cannot drift.
  269 |     await expect(page.locator('link[rel="apple-touch-icon"]')).toHaveAttribute(
  270 |       "href",
  271 |       "/static/apple-touch-icon.png",
  272 |     );
  273 |     await expect(page.locator('link[rel="manifest"]')).toHaveAttribute(
  274 |       "href",
  275 |       "/site.webmanifest",
  276 |     );
  277 |     await expect(page.locator('meta[name="theme-color"]')).toHaveAttribute(
  278 |       "content",
  279 |       "#0f172a",
  280 |     );
  281 |     await expect(
  282 |       page.locator('meta[name="apple-mobile-web-app-capable"]'),
  283 |     ).toHaveAttribute("content", "yes");
  284 |     // The bar carries the product name and the tabs, and nothing else: the
  285 |     // subtitle said "the request book" two lines above a heading that said the
  286 |     // same thing. Where you are is the highlighted tab and the page title.
  287 |     await expect(page.locator(".topbar")).toContainText("data-download");
  288 |     await expect(page.locator(".topbar .titles .micro")).toHaveCount(0);
  289 |   });
  290 | 
  291 |   test("the app mark sits at the left of the bar, before the name", async ({
  292 |     page,
  293 |   }) => {
  294 |     await mock(page);
  295 |     await page.goto("/");
  296 | 
  297 |     const mark = page.locator(".topbar .mark svg");
  298 |     await expect(mark).toBeVisible();
  299 |     await expect(mark).toHaveAttribute("aria-hidden", "true");
  300 | 
  301 |     // Before the wordmark, and on the same optical line: a 22px mark with the
  302 |     // 13px name to its right, not a badge stranded on top of the bar.
  303 |     const boxes = await page.evaluate(() => {
  304 |       const mark = document
  305 |         .querySelector(".topbar .mark")
  306 |         .getBoundingClientRect();
  307 |       const name = document
  308 |         .querySelector(".topbar .wordmark")
  309 |         .getBoundingClientRect();
  310 |       const svg = getComputedStyle(document.querySelector(".topbar .mark svg"));
  311 |       return {
  312 |         markLeft: mark.left,
  313 |         markHeight: mark.height,
  314 |         nameLeft: name.left,
  315 |         markMid: mark.top + mark.height / 2,
  316 |         nameMid: name.top + name.height / 2,
  317 |         colour: svg.color,
  318 |       };
  319 |     });
  320 |     expect(boxes.markLeft).toBeLessThan(boxes.nameLeft);
  321 |     expect(boxes.markHeight).toBeCloseTo(22, 0);
  322 |     expect(Math.abs(boxes.markMid - boxes.nameMid)).toBeLessThan(4);
  323 |     // The accent, not a baked hex: it follows the theme.
  324 |     expect(boxes.colour).toBe("rgb(125, 211, 252)");
  325 |   });
  326 | 
  327 |   test("the intro says what the book is, not what it cannot do", async ({
  328 |     page,
  329 |   }) => {
  330 |     // Two sentences were removed from above the list. "Phase 1: nothing
  331 |     // advances past draft or blocked. Waiting on a plan is not breakage."
  332 |     // explained the badge vocabulary, which the badge already speaks on the
  333 |     // row itself. "Nothing here can approve or change a request" described an
  334 |     // absence: the proof of it is that there is no button, and a reader who
  335 |     // came to look at the book was being told what the book is not.
  336 |     await mock(page);
  337 |     await page.goto("/");
  338 |     await expect(page.locator(".tbl-row")).toHaveCount(5);
  339 |     const body = await page.locator("body").innerText();
  340 |     expect(body).not.toContain("Phase 1");
  341 |     expect(body).not.toContain("not breakage");
  342 |     expect(body).not.toContain("approval is a CLI");
  343 |     expect(body).not.toContain("cannot reach");
  344 | 
  345 |     // The paragraph itself is gone too: three sentences of intro above six
  346 |     // rows was most of the page. The title says what the page is, the badge
  347 |     // says why a row matters, and the filter strip says what is in the book.
  348 |     await expect(page.locator(".sub-lede")).toHaveCount(0);
  349 |   });
  350 | 
```