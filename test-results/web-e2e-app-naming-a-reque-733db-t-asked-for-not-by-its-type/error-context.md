# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.
- Provide a snippet of code with the fix, if possible.

# Test info

- Name: web/e2e/app.spec.js >> naming a request, and opening it >> an intent is named by what it asked for, not by its type
- Location: web/e2e/app.spec.js:705:3

# Error details

```
Error: page.goto: Protocol error (Page.navigate): Cannot navigate to invalid URL
Call log:
  - navigating to "/", waiting until "load"

```

# Test source

```ts
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
  627 |     await page.goto("/");
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
> 709 |     await page.goto("/");
      |                ^ Error: page.goto: Protocol error (Page.navigate): Cannot navigate to invalid URL
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
  728 |     // A raw mouse click at the far end of the row -- the last cell -- because a
  729 |     // locator click would (correctly) refuse: the point now belongs to the
  730 |     // row-wide link, not to the badge it happens to be over.
  731 |     const box = await row.boundingBox();
  732 |     await page.mouse.click(box.x + box.width - 12, box.y + box.height / 2);
  733 | 
  734 |     await expect(page).toHaveURL(/\/requests\/req_0a7c9d33$/);
  735 |     await expect(page.locator(".detail-title")).toContainText("#4 ·");
  736 |   });
  737 | 
  738 |   test("clicking a row opens the request, with the why and the trail", async ({
  739 |     page,
  740 |   }) => {
  741 |     await mock(page);
  742 |     await page.goto("/");
  743 | 
  744 |     await page
  745 |       .locator(".tbl-row")
  746 |       .filter({ hasText: "OPRA.PILLAR · trades" })
  747 |       .locator("a.req")
  748 |       .click();
  749 |     await expect(page).toHaveURL(/\/requests\/req_0a7c9d33$/);
  750 | 
  751 |     await expect(page.locator(".detail-title")).toHaveText(
  752 |       "#4 · OPRA.PILLAR · trades · 2026-10-01→2026-10-08",
  753 |     );
  754 |     await expect(page.locator(".detail-header .pill")).toHaveText("Blocked");
  755 | 
  756 |     // The two things the list could not show: the human sentence, and the key
  757 |     // in full, because this is the page where someone copies it into a curl.
  758 |     await expect(page.locator(".why")).toHaveText(
  759 |       "OPRA.PILLAR trades for the Oct expiry cycle",
  760 |     );
  761 |     await expect(page.locator(".meta-grid")).toContainText("req_0a7c9d33");
  762 | 
  763 |     // The append-only trail, which no list view has room for.
  764 |     await expect(page.locator(".events li")).toHaveCount(1);
  765 |     await expect(page.locator(".events li")).toContainText("submitted");
  766 |     await expect(page.locator(".events li")).toContainText("submitter");
  767 | 
  768 |     // The body the caller sent, verbatim, behind a disclosure.
  769 |     await page.locator("details.raw summary").click();
  770 |     await expect(page.locator("details.raw pre")).toContainText("OPRA.PILLAR");
  771 |   });
  772 | 
  773 |   test("the request is read before the decision about it", async ({ page }) => {
  774 |     await mock(page, { detail: draftDetail });
  775 |     await page.goto("/requests/req_draft01");
  776 | 
  777 |     // The order is the argument: what was asked for, then why, then the verb.
  778 |     // "what was asked for" used to sit third, under the approve card -- the
  779 |     // reader got the decision before the thing being decided about.
  780 |     const order = await page
  781 |       .locator(".detail-card .block")
  782 |       .evaluateAll((blocks) =>
  783 |         blocks.map((block) =>
  784 |           block.querySelector(".micro")?.textContent.trim(),
  785 |         ),
  786 |       );
  787 |     expect(order.slice(0, 3)).toEqual(["what was asked for", "why", "spend"]);
  788 | 
  789 |     const request = page.locator(".block.request");
  790 |     // A request as a person says it: the list is a list and the window is a
  791 |     // range, not two JSON blobs shown one key at a time.
  792 |     await expect(request).toContainText("GLBX.MDP3");
  793 |     await expect(request).toContainText("ES.c.0");
  794 |     await expect(request).toContainText("2026-11-04 → 2026-11-06");
  795 |     await expect(request).not.toContainText('{"start"');
  796 | 
  797 |     // And the money, in the same block as the thing it prices.
  798 |     await expect(request.locator(".money")).toContainText("quoted $0.8115");
  799 |     await expect(request.locator(".money")).toContainText("ceiling $5");
  800 |   });
  801 | 
  802 |   test("an unpriced request says which kind of unpriced it is", async ({
  803 |     page,
  804 |   }) => {
  805 |     // The same row with no quote on it -- a blocked intent, which has no Job.
  806 |     await mock(page, {
  807 |       detail: {
  808 |         ...draftDetail,
  809 |         estimate_usd: null,
```