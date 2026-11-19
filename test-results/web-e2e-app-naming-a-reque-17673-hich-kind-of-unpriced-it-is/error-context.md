# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.
- Provide a snippet of code with the fix, if possible.

# Test info

- Name: web/e2e/app.spec.js >> naming a request, and opening it >> an unpriced request says which kind of unpriced it is
- Location: web/e2e/app.spec.js:802:3

# Error details

```
Error: page.goto: Protocol error (Page.navigate): Cannot navigate to invalid URL
Call log:
  - navigating to "/requests/req_draft01", waiting until "load"

```

# Test source

```ts
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
  810 |         blocked_reason: "awaiting_plan",
  811 |       },
  812 |     });
> 813 |     await page.goto("/requests/req_draft01");
      |                ^ Error: page.goto: Protocol error (Page.navigate): Cannot navigate to invalid URL
  814 |     // A dash would make three different absences look like one, and an
  815 |     // unpriced request look like a free one.
  816 |     await expect(page.locator(".block.request .money")).toContainText(
  817 |       "not priced — there is no job to quote",
  818 |     );
  819 |   });
  820 | 
  821 |   test("the detail page is a page: a reload and a back link both work", async ({
  822 |     page,
  823 |   }) => {
  824 |     await mock(page);
  825 |     await page.goto("/requests/req_0a7c9d33");
  826 |     await expect(page.locator(".detail-title")).toContainText("#4 ·");
  827 |     await page.reload();
  828 |     await expect(page.locator(".detail-title")).toContainText("#4 ·");
  829 |     await expect(page.locator(".back-btn")).toBeVisible();
  830 |   });
  831 | });
  832 | 
  833 | test.describe("approving a request", () => {
  834 |   // The page used to have no verb at all. It has exactly one now, and it is the
  835 |   // one that spends money. It asked for an `approve` token and a name; it now
  836 |   // asks the server who the tailnet hop says you are, so the card can neither
  837 |   // leak a secret nor be handed one.
  838 |   const noteField = (page) => page.locator('.approve input[type="text"]');
  839 |   const approveButton = (page) => page.locator(".approve .btn.primary");
  840 | 
  841 |   async function interceptApprove(page, status = 200, body = null) {
  842 |     const seen = [];
  843 |     await page.route("**/v1/requests/**/approve", async (route) => {
  844 |       seen.push({
  845 |         headers: route.request().headers(),
  846 |         body: route.request().postDataJSON(),
  847 |       });
  848 |       await route.fulfill({
  849 |         status,
  850 |         contentType: "application/json",
  851 |         body: JSON.stringify(
  852 |           body ?? {
  853 |             request_id: "req_draft01",
  854 |             state: "approved",
  855 |             job_hash: "a3d57f9b",
  856 |             approved: true,
  857 |           },
  858 |         ),
  859 |       });
  860 |     });
  861 |     return seen;
  862 |   }
  863 | 
  864 |   test("the card names the approver and asks for nothing", async ({ page }) => {
  865 |     await mock(page, { detail: draftDetail });
  866 |     await page.goto("/requests/req_draft01");
  867 | 
  868 |     await expect(page.locator(".approve .who")).toContainText(
  869 |       "Approving as nick@example.com.",
  870 |     );
  871 |     // No credential field and no name field: both would be a second identity.
  872 |     await expect(page.locator('.approve input[type="password"]')).toHaveCount(
  873 |       0,
  874 |     );
  875 |     await expect(page.locator(".approve .fields label")).toHaveCount(1);
  876 |     await expect(approveButton(page)).toBeEnabled();
  877 |     await expect(approveButton(page)).toContainText("$5");
  878 |   });
  879 | 
  880 |   test("the ceiling is read between the click and the spend", async ({
  881 |     page,
  882 |   }) => {
  883 |     const seen = await interceptApprove(page);
  884 |     await mock(page, { detail: draftDetail });
  885 |     await page.goto("/requests/req_draft01");
  886 | 
  887 |     await noteField(page).fill("ES two days");
  888 |     await approveButton(page).click();
  889 | 
  890 |     // First click asks; it does not post.
  891 |     await expect(page.locator(".approve .ask")).toContainText("$5");
  892 |     expect(seen).toHaveLength(0);
  893 | 
  894 |     await approveButton(page).click();
  895 |     await expect.poll(() => seen.length).toBe(1);
  896 |     // No credential travels: the route would refuse one anyway.
  897 |     expect(seen[0].headers.authorization).toBeUndefined();
  898 |     expect(seen[0].body).toEqual({ note: "ES two days" });
  899 |     await expect(page).not.toHaveURL(/token=/);
  900 |   });
  901 | 
  902 |   test("the token this card used to ask for is scrubbed from the browser", async ({
  903 |     page,
  904 |   }) => {
  905 |     await mock(page, { detail: draftDetail });
  906 |     await page.goto("/requests/req_draft01");
  907 |     await page.evaluate(() => {
  908 |       window.localStorage.setItem("data-download.approve-token", "tok-approve");
  909 |       window.localStorage.setItem("data-download.actor", "nick");
  910 |     });
  911 |     await page.reload();
  912 | 
  913 |     // A secret left in storage after its door was removed reads as live. The
```