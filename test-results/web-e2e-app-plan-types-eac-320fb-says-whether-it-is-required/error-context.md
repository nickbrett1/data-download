# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.
- Provide a snippet of code with the fix, if possible.

# Test info

- Name: web/e2e/app.spec.js >> plan types >> each parameter says whether it is required
- Location: web/e2e/app.spec.js:454:3

# Error details

```
Error: page.goto: Protocol error (Page.navigate): Cannot navigate to invalid URL
Call log:
  - navigating to "/plan-types", waiting until "load"

```

# Test source

```ts
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
  381 |     await page.goto("/");
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
> 456 |     await page.goto("/plan-types");
      |                ^ Error: page.goto: Protocol error (Page.navigate): Cannot navigate to invalid URL
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
  482 |       .locator(".tbl-row")
  483 |       .first()
  484 |       .evaluate((row) => {
  485 |         const name = row.querySelector(".name").getBoundingClientRect();
  486 |         const tag = row.querySelector(".req").getBoundingClientRect();
  487 |         return { nameRight: name.right, tagRight: tag.right };
  488 |       });
  489 |     expect(box.tagRight).toBeLessThanOrEqual(box.nameRight + 1);
  490 |   });
  491 | 
  492 |   test("the card is the table and the raw schema, and no glossary under it", async ({
  493 |     page,
  494 |   }) => {
  495 |     // A definition list used to sit under each table: extra fields, verify,
  496 |     // resume, source. Four labels of prose about a schema, on a page whose job
  497 |     // is to answer "which fields does this plan take?". It went, and the card
  498 |     // is now the description, the parameter rows, and the collapsed schema -
  499 |     // the last of which is the document itself, so nothing is lost.
  500 |     await mock(page);
  501 |     await page.goto("/plan-types");
  502 | 
  503 |     await expect(page.locator(".kv")).toHaveCount(0);
  504 |     const body = await page.locator("body").innerText();
  505 |     for (const gone of [
  506 |       "extra fields",
  507 |       "verify",
  508 |       "resume",
  509 |       "Rejected",
  510 |       "Accepted",
  511 |     ]) {
  512 |       expect(body.toLowerCase()).not.toContain(gone.toLowerCase());
  513 |     }
  514 | 
  515 |     const first = page.locator(".type-card").first();
  516 |     await expect(first.locator(".tbl-row")).toHaveCount(3);
  517 |     // The raw schema still declares its dialect, so a copy-paste has the rules.
  518 |     await expect(first.locator("details pre")).toContainText("draft/2020-12");
  519 |   });
  520 | 
  521 |   test("nothing on the page can change the registry", async ({ page }) => {
  522 |     await mock(page);
  523 |     await page.goto("/plan-types");
  524 |     // The registry is git, not the database: a control here would be a lie.
  525 |     await expect(page.locator("form")).toHaveCount(0);
  526 |     await expect(page.locator("input")).toHaveCount(0);
  527 |     // The other page is one click away, and the token rides along or it 401s.
  528 |     await expect(page.locator(".main-nav a").first()).toHaveText("Requests");
  529 |   });
  530 | 
  531 |   test("an empty registry says every submission will be rejected", async ({
  532 |     page,
  533 |   }) => {
  534 |     await mock(page, {
  535 |       types: { types: [] },
  536 |     });
  537 |     await page.goto("/plan-types");
  538 |     await expect(page.locator(".empty-state")).toContainText(
  539 |       "every submission will be rejected",
  540 |     );
  541 |   });
  542 | });
  543 | 
  544 | // The regression that made the Svelte port worse than the Jinja pages on a
  545 | // phone: navigation was a state pill (a destination that looked like a badge),
  546 | // and the page intro had no type because `.lede` was never defined. Both are
  547 | // measurable, so they are measured here at 390px rather than eyeballed.
  548 | test.describe("the shell on a phone", () => {
  549 |   test.use({ viewport: { width: 390, height: 780 } });
  550 | 
  551 |   async function expectTabs(page, current) {
  552 |     const tabs = page.locator(".main-nav a");
  553 |     await expect(tabs).toHaveCount(2);
  554 |     await expect(tabs).toHaveText(["Requests", "Plan types"]);
  555 |     // Exactly one tab is the current page, and it is the right one.
  556 |     await expect(page.locator('.main-nav a[aria-current="page"]')).toHaveCount(
```