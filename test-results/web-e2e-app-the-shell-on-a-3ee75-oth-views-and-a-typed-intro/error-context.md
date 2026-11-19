# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.
- Provide a snippet of code with the fix, if possible.

# Test info

- Name: web/e2e/app.spec.js >> the shell on a phone >> the request book shows both views and a typed intro
- Location: web/e2e/app.spec.js:623:3

# Error details

```
Error: page.goto: Protocol error (Page.navigate): Cannot navigate to invalid URL
Call log:
  - navigating to "/", waiting until "load"

```

# Test source

```ts
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
  624 |     page,
  625 |   }) => {
  626 |     await mock(page);
> 627 |     await page.goto("/");
      |                ^ Error: page.goto: Protocol error (Page.navigate): Cannot navigate to invalid URL
  628 |     await expect(page.locator(".tbl-row")).toHaveCount(5);
  629 |     await expectTabs(page, "Requests");
  630 |     await expectLede(page, "The request book");
  631 |     await expectNoHorizontalScroll(page);
  632 |   });
  633 | 
  634 |   test("plan types shows both views and a typed intro", async ({ page }) => {
  635 |     await mock(page);
  636 |     await page.goto("/plan-types");
  637 |     await expect(page.locator(".type-card")).toHaveCount(2);
  638 |     await expectTabs(page, "Plan types");
  639 |     await expectLede(page, "Plan types");
  640 |     await expectNoHorizontalScroll(page);
  641 |   });
  642 | 
  643 |   test("on a notched home screen the bar does not eat the intro", async ({
  644 |     page,
  645 |   }) => {
  646 |     await mock(page);
  647 |     await page.goto("/");
  648 |     await expect(page.locator(".tbl-row")).toHaveCount(5);
  649 |     await expectSafeAreaDoesNotCoverLede(page);
  650 |   });
  651 | 
  652 |   test("the plan-types intro also clears a notched bar", async ({ page }) => {
  653 |     await mock(page);
  654 |     await page.goto("/plan-types");
  655 |     await expect(page.locator(".type-card")).toHaveCount(2);
  656 |     await expectSafeAreaDoesNotCoverLede(page);
  657 |   });
  658 | });
  659 | 
  660 | test.describe("the page ends where the content ends", () => {
  661 |   // The footer used to hold a generated-at stamp, the store path, and a link
  662 |   // to the other tab. The link duplicated the nav directly above it, the
  663 |   // timestamp always read "now" on a page that is never cached, and the path
  664 |   // is the host's business. A page that is one list should stop after the list.
  665 |   for (const path of ["/", "/plan-types"]) {
  666 |     test(`${path} has no footer and no server trivia`, async ({ page }) => {
  667 |       await mock(page);
  668 |       await page.goto(path);
  669 | 
  670 |       await expect(page.locator("footer")).toHaveCount(0);
  671 |       const body = await page.locator("body").innerText();
  672 |       expect(body).not.toContain("generated");
  673 |       expect(body).not.toContain("read-only, no authority");
  674 |       expect(body).not.toContain("data-download.sqlite");
  675 | 
  676 |       // The navigation exists once, in the bar.
  677 |       await expect(page.locator(".main-nav")).toHaveCount(1);
  678 |       await expect(page.locator(".main-nav a")).toHaveCount(2);
  679 |     });
  680 |   }
  681 | });
  682 | 
  683 | test.describe("naming a request, and opening it", () => {
  684 |   test("a row leads with a name, not with the key", async ({ page }) => {
  685 |     await mock(page);
  686 |     await page.goto("/");
  687 | 
  688 |     const first = page.locator(".tbl-row").first();
  689 |     // `#1` and what was asked for. The 32-hex key is the tooltip and nothing
  690 |     // else in the row: it is a durable handle, and a handle is not a name.
  691 |     await expect(first.locator(".req-num")).toHaveText("#1");
  692 |     await expect(first.locator(".req-subject")).toHaveText(
  693 |       "databento_batch_download",
  694 |     );
  695 |     await expect(first.locator(".req-id")).toHaveCount(0);
  696 |     await expect(first).not.toContainText("9f2a41c8");
  697 |     // Who is being asked. A dataset is vendor vocabulary; the name is context.
  698 |     await expect(first.locator(".req-vendor")).toHaveText("Databento");
  699 | 
  700 |     const link = first.locator("a.req");
  701 |     await expect(link).toHaveAttribute("href", /\/requests\/req_9f2a41c8$/);
  702 |     await expect(link).toHaveAttribute("title", "req_9f2a41c8");
  703 |   });
  704 | 
  705 |   test("an intent is named by what it asked for, not by its type", async ({
  706 |     page,
  707 |   }) => {
  708 |     await mock(page);
  709 |     await page.goto("/");
  710 |     const intentRow = page.locator(".tbl-row").filter({
  711 |       hasText: "OPRA.PILLAR · trades",
  712 |     });
  713 |     await expect(intentRow.locator(".req-subject")).toHaveText(
  714 |       "OPRA.PILLAR · trades · 2026-10-01→2026-10-08",
  715 |     );
  716 |     // An intent has no plan type, so its vendor comes from the registry's
  717 |     // dataset namespace — the same word the plan rows show.
  718 |     await expect(intentRow.locator(".req-vendor")).toHaveText("Databento");
  719 |   });
  720 | 
  721 |   test("the whole row is the target, not just the name", async ({ page }) => {
  722 |     await mock(page);
  723 |     await page.goto("/");
  724 | 
  725 |     // The far end of the row -- the badge in the last column -- which used to be
  726 |     // dead space. The anchor is still the only link; its `::after` covers the row.
  727 |     const row = page.locator(".tbl-row").filter({ hasText: "OPRA.PILLAR" });
```