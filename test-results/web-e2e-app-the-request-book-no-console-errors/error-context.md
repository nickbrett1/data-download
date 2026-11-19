# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.
- Provide a snippet of code with the fix, if possible.

# Test info

- Name: web/e2e/app.spec.js >> the request book >> no console errors
- Location: web/e2e/app.spec.js:374:3

# Error details

```
Error: page.goto: Protocol error (Page.navigate): Cannot navigate to invalid URL
Call log:
  - navigating to "/", waiting until "load"

```

# Test source

```ts
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
  351 |   test("an empty book says it once", async ({ page }) => {
  352 |     // It used to say it three times: the block's "zero results" label, "No
  353 |     // requests yet.", and the block's stock hint - under a filter strip of six
  354 |     // zeroes. One sentence, and the only one the stock hint could not say:
  355 |     // nothing on this page puts a request in the book.
  356 |     await mock(page, { requests: emptyBook });
  357 |     await page.goto("/");
  358 | 
  359 |     // The door is the API, not a CLI: this service ships no CLI binary, and the
  360 |     // first version of this sentence said "filed from the CLI", which was an
  361 |     // invention. `POST /v1/requests` is what actually creates a row.
  362 |     await expect(page.locator(".empty-book")).toHaveText(
  363 |       /^No requests yet — requests arrive through the API \(POST \/v1\/requests\), not from this page\.$/,
  364 |     );
  365 |     const body = await page.locator("body").innerText();
  366 |     expect(body.toLowerCase()).not.toContain("zero results");
  367 |     expect(body).not.toContain("No items have been added");
  368 |     expect(body.match(/No requests yet/g)).toHaveLength(1);
  369 | 
  370 |     // Nothing to narrow, so no strip of zeroes above the sentence.
  371 |     await expect(page.locator(".filter-panel")).toHaveCount(0);
  372 |   });
  373 | 
  374 |   test("no console errors", async ({ page }) => {
  375 |     const errors = [];
  376 |     page.on(
  377 |       "console",
  378 |       (msg) => msg.type() === "error" && errors.push(msg.text()),
  379 |     );
  380 |     await mock(page);
> 381 |     await page.goto("/");
      |                ^ Error: page.goto: Protocol error (Page.navigate): Cannot navigate to invalid URL
  382 |     await expect(page.locator(".tbl-row")).toHaveCount(5);
  383 |     expect(errors).toEqual([]);
  384 |   });
  385 | 
  386 |   test("on a phone the header row goes and each record wraps", async ({
  387 |     page,
  388 |   }) => {
  389 |     await mock(page);
  390 |     await page.setViewportSize({ width: 390, height: 844 });
  391 |     await page.goto("/");
  392 |     await expect(page.locator(".tbl-head")).toBeHidden();
  393 |     // Six columns on a phone is a scrollbar or a truncated id; the record wraps
  394 |     // instead, and stays a record.
  395 |     const display = await page
  396 |       .locator(".tbl-row")
  397 |       .first()
  398 |       .evaluate((el) => getComputedStyle(el).flexWrap);
  399 |     expect(display).toBe("wrap");
  400 |   });
  401 | });
  402 | 
  403 | test.describe("plan types", () => {
  404 |   test("every type is a card with a table of its parameters", async ({
  405 |     page,
  406 |   }) => {
  407 |     await mock(page);
  408 |     await page.goto("/plan-types");
  409 | 
  410 |     await expect(page.locator(".type-card")).toHaveCount(2);
  411 |     await expect(page.locator(".toc a")).toHaveText([
  412 |       "databento_batch_download",
  413 |       "http_fetch",
  414 |     ]);
  415 | 
  416 |     const first = page.locator(".type-card").first();
  417 |     await expect(first).toContainText("2 required · 1 optional");
  418 |     await expect(first.locator(".tbl-row")).toHaveCount(3);
  419 |     await expect(first.locator(".tbl-head .col-label")).toHaveText([
  420 |       "parameter",
  421 |       "type",
  422 |       "constraints",
  423 |       "what it means",
  424 |     ]);
  425 |     // The enum is spelled out, not hidden behind a schema reference: that is the
  426 |     // question the page exists to answer.
  427 |     await expect(first).toContainText("enum: GLBX.MDP3, XNAS.ITCH");
  428 |     // A field with no description is an em dash, not an empty cell.
  429 |     await expect(first.locator(".tbl-row").nth(2).locator(".desc")).toHaveText(
  430 |       "—",
  431 |     );
  432 |   });
  433 | 
  434 |   // The Svelte port kept the header count and dropped the per-field marker, so
  435 |   // the page said *how many* parameters were required but not *which*: a reader
  436 |   // with three fields had to open the raw JSON to find out. The count is not a
  437 |   // substitute for the tag, so both are asserted here.
  438 |   async function expectRequiredMarkers(page, card) {
  439 |     await expect(card.locator(".type-head .micro")).toHaveText(
  440 |       "2 required · 1 optional",
  441 |     );
  442 | 
  443 |     const required = card.locator(".tbl-row").filter({ hasText: "dataset" });
  444 |     // Visible, not merely present: the marker sits inline after the name
  445 |     // because DataTable drops the header row under 600px, and a hidden tag
  446 |     // would be the same regression in a quieter form.
  447 |     await expect(required.locator(".req")).toBeVisible();
  448 |     await expect(required.locator(".req")).toHaveText("required");
  449 | 
  450 |     const optional = card.locator(".tbl-row").filter({ hasText: "symbols" });
  451 |     await expect(optional.locator(".req")).toHaveCount(0);
  452 |   }
  453 | 
  454 |   test("each parameter says whether it is required", async ({ page }) => {
  455 |     await mock(page);
  456 |     await page.goto("/plan-types");
  457 | 
  458 |     await expectRequiredMarkers(page, page.locator(".type-card").first());
  459 |     // Property order stays the YAML's fill-in order: required first is a
  460 |     // coincidence here (`dataset`, `start`), not a rule the page imposes.
  461 |     await expect(
  462 |       page.locator(".type-card").first().locator(".tbl-row .name code"),
  463 |     ).toHaveText(["dataset", "start", "symbols"]);
  464 |   });
  465 | 
  466 |   test("the marker survives a phone, where the header row does not", async ({
  467 |     page,
  468 |   }) => {
  469 |     await mock(page);
  470 |     await page.setViewportSize({ width: 390, height: 844 });
  471 |     await page.goto("/plan-types");
  472 | 
  473 |     const first = page.locator(".type-card").first();
  474 |     // The reason the marker is inline and not a fifth column.
  475 |     await expect(first.locator(".tbl-head")).toBeHidden();
  476 |     await expectRequiredMarkers(page, first);
  477 | 
  478 |     // DataTable truncates every cell (`.tbl-row :global(*)`), and the tag sits
  479 |     // inside `.name`: an overflowing cell would clip it while `toBeVisible()`
  480 |     // still passed, since clipping does not zero a bounding box.
  481 |     const box = await first
```