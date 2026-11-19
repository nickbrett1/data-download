# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.
- Provide a snippet of code with the fix, if possible.

# Test info

- Name: web/e2e/app.spec.js >> approving a request >> a login that may read but not spend gets no button
- Location: web/e2e/app.spec.js:942:3

# Error details

```
Error: page.goto: Protocol error (Page.navigate): Cannot navigate to invalid URL
Call log:
  - navigating to "/requests/req_draft01", waiting until "load"

```

# Test source

```ts
  849  |         status,
  850  |         contentType: "application/json",
  851  |         body: JSON.stringify(
  852  |           body ?? {
  853  |             request_id: "req_draft01",
  854  |             state: "approved",
  855  |             job_hash: "a3d57f9b",
  856  |             approved: true,
  857  |           },
  858  |         ),
  859  |       });
  860  |     });
  861  |     return seen;
  862  |   }
  863  | 
  864  |   test("the card names the approver and asks for nothing", async ({ page }) => {
  865  |     await mock(page, { detail: draftDetail });
  866  |     await page.goto("/requests/req_draft01");
  867  | 
  868  |     await expect(page.locator(".approve .who")).toContainText(
  869  |       "Approving as nick@example.com.",
  870  |     );
  871  |     // No credential field and no name field: both would be a second identity.
  872  |     await expect(page.locator('.approve input[type="password"]')).toHaveCount(
  873  |       0,
  874  |     );
  875  |     await expect(page.locator(".approve .fields label")).toHaveCount(1);
  876  |     await expect(approveButton(page)).toBeEnabled();
  877  |     await expect(approveButton(page)).toContainText("$5");
  878  |   });
  879  | 
  880  |   test("the ceiling is read between the click and the spend", async ({
  881  |     page,
  882  |   }) => {
  883  |     const seen = await interceptApprove(page);
  884  |     await mock(page, { detail: draftDetail });
  885  |     await page.goto("/requests/req_draft01");
  886  | 
  887  |     await noteField(page).fill("ES two days");
  888  |     await approveButton(page).click();
  889  | 
  890  |     // First click asks; it does not post.
  891  |     await expect(page.locator(".approve .ask")).toContainText("$5");
  892  |     expect(seen).toHaveLength(0);
  893  | 
  894  |     await approveButton(page).click();
  895  |     await expect.poll(() => seen.length).toBe(1);
  896  |     // No credential travels: the route would refuse one anyway.
  897  |     expect(seen[0].headers.authorization).toBeUndefined();
  898  |     expect(seen[0].body).toEqual({ note: "ES two days" });
  899  |     await expect(page).not.toHaveURL(/token=/);
  900  |   });
  901  | 
  902  |   test("the token this card used to ask for is scrubbed from the browser", async ({
  903  |     page,
  904  |   }) => {
  905  |     await mock(page, { detail: draftDetail });
  906  |     await page.goto("/requests/req_draft01");
  907  |     await page.evaluate(() => {
  908  |       window.localStorage.setItem("data-download.approve-token", "tok-approve");
  909  |       window.localStorage.setItem("data-download.actor", "nick");
  910  |     });
  911  |     await page.reload();
  912  | 
  913  |     // A secret left in storage after its door was removed reads as live. The
  914  |     // page that asked for it is the page that deletes it.
  915  |     const left = await page.evaluate(() => [
  916  |       window.localStorage.getItem("data-download.approve-token"),
  917  |       window.localStorage.getItem("data-download.actor"),
  918  |     ]);
  919  |     expect(left).toEqual([null, null]);
  920  |   });
  921  | 
  922  |   test("a refusal is the server's, in the server's words", async ({ page }) => {
  923  |     await interceptApprove(page, 403, {
  924  |       error: {
  925  |         code: "not_an_approver",
  926  |         message:
  927  |           "this tailnet login is not named in DATA_DOWNLOAD_APPROVERS, " +
  928  |           "so it may read but not approve",
  929  |       },
  930  |     });
  931  |     await mock(page, { detail: draftDetail });
  932  |     await page.goto("/requests/req_draft01");
  933  | 
  934  |     await approveButton(page).click();
  935  |     await approveButton(page).click();
  936  | 
  937  |     await expect(page.locator(".approve .failure")).toContainText(
  938  |       "not named in DATA_DOWNLOAD_APPROVERS",
  939  |     );
  940  |   });
  941  | 
  942  |   test("a login that may read but not spend gets no button", async ({
  943  |     page,
  944  |   }) => {
  945  |     await mock(page, {
  946  |       detail: draftDetail,
  947  |       who: { login: "someone-else@example.com", approver: false },
  948  |     });
> 949  |     await page.goto("/requests/req_draft01");
       |                ^ Error: page.goto: Protocol error (Page.navigate): Cannot navigate to invalid URL
  950  | 
  951  |     await expect(page.locator(".approve .who")).toContainText(
  952  |       "Approving as someone-else@example.com.",
  953  |     );
  954  |     await expect(page.locator(".approve .who")).toContainText(
  955  |       "may read but not approve",
  956  |     );
  957  |     await expect(approveButton(page)).toHaveCount(0);
  958  |   });
  959  | 
  960  |   test("a page that did not come through serve offers no verb", async ({
  961  |     page,
  962  |   }) => {
  963  |     await mock(page, { detail: draftDetail, whoStatus: 403 });
  964  |     await page.goto("/requests/req_draft01");
  965  | 
  966  |     // Not a disabled button: nothing here could be fixed by clicking, so the
  967  |     // only honest thing to render is why.
  968  |     await expect(approveButton(page)).toHaveCount(0);
  969  |     await expect(page.locator(".approve .failure")).toContainText(
  970  |       "no Tailscale-User-Login header",
  971  |     );
  972  |   });
  973  | 
  974  |   test("an approved row has nothing to click", async ({ page }) => {
  975  |     await mock(page, {
  976  |       detail: { ...draftDetail, state: "approved", approved_unclaimed: true },
  977  |     });
  978  |     await page.goto("/requests/req_draft01");
  979  |     await expect(page.locator(".approve")).toHaveCount(0);
  980  |     await expect(page.locator(".detail-header .pill")).toHaveText("Approved");
  981  |     // The book's sentence, in full, on the page it was clicked through to. A
  982  |     // different vocabulary here would be a second answer to one question.
  983  |     await expect(page.locator(".meta-grid")).toContainText("worker");
  984  |     await expect(page.locator(".meta-grid")).toContainText(
  985  |       "none — no worker has picked it up",
  986  |     );
  987  |   });
  988  | 
  989  |   test("a blocked row has nothing to click either", async ({ page }) => {
  990  |     await mock(page);
  991  |     await page.goto("/requests/req_0a7c9d33");
  992  |     await expect(page.locator(".approve")).toHaveCount(0);
  993  |   });
  994  | });
  995  | 
  996  | test.describe("the bar at the top of the page", () => {
  997  |   // This describe block used to be "the bar as a Home Screen app". app-shell's
  998  |   // bar was glass, so this app patched it under `display-mode: standalone` and
  999  |   // under `html.ios-standalone` (set inline in index.html from
  1000 |   // `navigator.standalone`). @nb 1.16.1 made the bar opaque everywhere -- a bar
  1001 |   // that owns the top edge of the viewport has nothing above it to see through
  1002 |   // to -- and the patch is gone with it.
  1003 |   //
  1004 |   // So this asserts the block's own rule on an ordinary page load, with nothing
  1005 |   // emulated, because that is now the only case there is. The regression it
  1006 |   // guards has not gone away, it has moved: it is a consumer stylesheet putting
  1007 |   // the glass back for specificity, which is precisely what the deleted patch
  1008 |   // did in the other direction.
  1009 |   for (const [path, name] of [
  1010 |     ["/", "the request book"],
  1011 |     ["/plan-types", "plan types"],
  1012 |   ]) {
  1013 |     test(`${name}: the bar is opaque, with no blur`, async ({ page }) => {
  1014 |       await mock(page);
  1015 |       await page.goto(path);
  1016 | 
  1017 |       // Only the standard property: Chromium does not serialise the
  1018 |       // `-webkit-` alias in computed style, so asserting on it proves nothing.
  1019 |       const bar = await page.locator(".topbar").evaluate((el) => {
  1020 |         const style = getComputedStyle(el);
  1021 |         return {
  1022 |           backdrop: style.backdropFilter,
  1023 |           background: style.backgroundColor,
  1024 |         };
  1025 |       });
  1026 | 
  1027 |       expect(bar.backdrop).toBe("none");
  1028 |       // `background` is a var(--bg) shorthand, so an opaque colour is the whole
  1029 |       // assertion: a percentage here is the bug coming back.
  1030 |       expect(bar.background).toBe("rgb(15, 23, 42)");
  1031 |     });
  1032 |   }
  1033 | 
  1034 |   test("it is the same while content is scrolling under it", async ({
  1035 |     page,
  1036 |   }) => {
  1037 |     await mock(page);
  1038 |     await page.goto("/");
  1039 |     await page.evaluate(() => window.scrollTo(0, 600));
  1040 |     await expect
  1041 |       .poll(() =>
  1042 |         page
  1043 |           .locator(".topbar")
  1044 |           .evaluate((el) => getComputedStyle(el).backgroundColor),
  1045 |       )
  1046 |       .toBe("rgb(15, 23, 42)");
  1047 |     const backdrop = await page
  1048 |       .locator(".topbar")
  1049 |       .evaluate((el) => getComputedStyle(el).backdropFilter);
```