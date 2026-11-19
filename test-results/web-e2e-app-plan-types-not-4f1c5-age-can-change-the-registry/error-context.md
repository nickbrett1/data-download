# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.
- Provide a snippet of code with the fix, if possible.

# Test info

- Name: web/e2e/app.spec.js >> plan types >> nothing on the page can change the registry
- Location: web/e2e/app.spec.js:521:3

# Error details

```
Error: page.goto: Protocol error (Page.navigate): Cannot navigate to invalid URL
Call log:
  - navigating to "/plan-types", waiting until "load"

```

# Test source

```ts
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
> 523 |     await page.goto("/plan-types");
      |                ^ Error: page.goto: Protocol error (Page.navigate): Cannot navigate to invalid URL
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
  557 |       1,
  558 |     );
  559 |     await expect(page.locator('.main-nav a[aria-current="page"]')).toHaveText(
  560 |       current,
  561 |     );
  562 |     // Both tabs are reachable without scrolling the bar: a tab row that hides
  563 |     // the second view is the bug this test exists for.
  564 |     const fits = await page
  565 |       .locator(".main-nav")
  566 |       .evaluate((el) => el.scrollWidth <= el.clientWidth + 1);
  567 |     expect(fits).toBe(true);
  568 |   }
  569 | 
  570 |   async function expectLede(page, text) {
  571 |     const lede = page.locator(".lede");
  572 |     await expect(lede).toHaveText(text);
  573 |     const type = await lede.evaluate((el) => {
  574 |       const style = getComputedStyle(el);
  575 |       return { size: style.fontSize, weight: style.fontWeight };
  576 |     });
  577 |     expect(type.size).toBe("15px");
  578 |     expect(type.weight).toBe("600");
  579 |   }
  580 | 
  581 |   async function expectNoHorizontalScroll(page) {
  582 |     const overflow = await page.evaluate(
  583 |       () => document.documentElement.scrollWidth - window.innerWidth,
  584 |     );
  585 |     expect(overflow).toBeLessThanOrEqual(0);
  586 |   }
  587 | 
  588 |   // Chromium reports `env(safe-area-inset-top)` as 0 on every machine without a
  589 |   // notch, so the inset is injected where the shell reads it: a custom property
  590 |   // on the document element. 47px is the value a notched iPhone reports. With
  591 |   // the sticky bar at `top: var(--safe-top)` (1.10.0) the bar is painted 47px
  592 |   // lower while its flow box stays put, covering the intro completely: the
  593 |   // bar's bottom lands *below* the lede's top. `padding-top` (1.10.1) grows
  594 |   // the bar instead, so the flow moves with it. Both facts are measured below
  595 |   // rather than deduced from a screenshot.
  596 |   async function expectSafeAreaDoesNotCoverLede(page) {
  597 |     await expect(page.locator(".lede")).toBeVisible();
  598 |     await page.evaluate(() => {
  599 |       document.documentElement.style.setProperty("--safe-top", "47px");
  600 |       window.scrollTo(0, 0);
  601 |     });
  602 | 
  603 |     const measured = await page.evaluate(() => {
  604 |       const bar = document.querySelector(".topbar").getBoundingClientRect();
  605 |       const ledeEl = document.querySelector(".lede");
  606 |       const lede = ledeEl.getBoundingClientRect();
  607 |       const hit = document.elementFromPoint(
  608 |         lede.left + lede.width / 2,
  609 |         lede.top + lede.height / 2,
  610 |       );
  611 |       return {
  612 |         barBottom: bar.bottom,
  613 |         ledeTop: lede.top,
  614 |         ledeHit: Boolean(hit?.closest(".lede")),
  615 |       };
  616 |     });
  617 | 
  618 |     expect(measured.ledeTop).toBeGreaterThanOrEqual(measured.barBottom);
  619 |     expect(measured.ledeHit).toBe(true);
  620 |     await expectNoHorizontalScroll(page);
  621 |   }
  622 | 
  623 |   test("the request book shows both views and a typed intro", async ({
```