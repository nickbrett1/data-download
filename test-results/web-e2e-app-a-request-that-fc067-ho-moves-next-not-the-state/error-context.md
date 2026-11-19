# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.
- Provide a snippet of code with the fix, if possible.

# Test info

- Name: web/e2e/app.spec.js >> a request that is waiting, in three different ways >> the facets name who moves next, not the state
- Location: web/e2e/app.spec.js:1058:3

# Error details

```
Error: page.goto: Protocol error (Page.navigate): Cannot navigate to invalid URL
Call log:
  - navigating to "/", waiting until "load"

```

# Test source

```ts
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
  1050 |     expect(backdrop).toBe("none");
  1051 |   });
  1052 | });
  1053 | 
  1054 | // One blocked row used to carry one label. It carried three different
  1055 | // situations with three different next moves, and a reader could not tell "add
  1056 | // two fields" from "this needs new code" — the question that started phase 2.5.
  1057 | test.describe("a request that is waiting, in three different ways", () => {
  1058 |   test("the facets name who moves next, not the state", async ({ page }) => {
  1059 |     await mock(page, { requests: blockedBook });
> 1060 |     await page.goto("/");
       |                ^ Error: page.goto: Protocol error (Page.navigate): Cannot navigate to invalid URL
  1061 | 
  1062 |     const group = page.locator(".facet-group").first();
  1063 |     // Each blocked answer is its own choice, one row apiece.
  1064 |     for (const [label, count] of [
  1065 |       ["Needs parameters", "1"],
  1066 |       ["No plan yet", "1"],
  1067 |       ["Plan requested", "1"],
  1068 |     ]) {
  1069 |       const option = group
  1070 |         .locator(".facet-option")
  1071 |         .filter({ hasText: label })
  1072 |         .first();
  1073 |       await expect(option).toBeVisible();
  1074 |       await expect(option).toContainText(count);
  1075 |     }
  1076 |     // The old name is gone rather than aliased: two words for one facet would
  1077 |     // be two filters for one set of rows.
  1078 |     await expect(page.locator("body")).not.toContainText("Waiting on a plan");
  1079 |   });
  1080 | 
  1081 |   test("each blocked row says what would clear it", async ({ page }) => {
  1082 |     await mock(page, { requests: blockedBook });
  1083 |     await page.goto("/");
  1084 | 
  1085 |     const row = (seq) =>
  1086 |       page.locator(".tbl-row").filter({ hasText: `#${seq}` });
  1087 |     // All three are `blocked`, so all three badges say the same word; the line
  1088 |     // under it is what tells them apart, and it is the only place on the page
  1089 |     // that says what would clear the row.
  1090 |     for (const seq of [1, 2, 3]) {
  1091 |       await expect(row(seq).locator(".pill")).toHaveText("Blocked");
  1092 |     }
  1093 |     // The caller's move: the two params the plan is short of.
  1094 |     await expect(row(1).locator(".state .detail")).toHaveText(
  1095 |       "add symbols, stype_in",
  1096 |     );
  1097 |     // No registered type speaks for the dataset. Nothing the caller can add.
  1098 |     await expect(row(2).locator(".state .detail")).toHaveText(
  1099 |       "no plan for ZZZZ.UNKNOWN",
  1100 |     );
  1101 |     // The ask is in, so the row is no longer "no plan yet".
  1102 |     await expect(row(3).locator(".state .detail")).toHaveText(
  1103 |       "new plan requested",
  1104 |     );
  1105 |     // The timestamp is the tooltip, not a second line: the detail is what
  1106 |     // clears the row, and the row already has a `filed` column.
  1107 |     await expect(row(3).locator(".state .detail")).toHaveAttribute(
  1108 |       "title",
  1109 |       /asked for on 14 Nov/,
  1110 |     );
  1111 |   });
  1112 | 
  1113 |   test("filtering on one of them narrows to exactly that row", async ({
  1114 |     page,
  1115 |   }) => {
  1116 |     await mock(page, { requests: blockedBook });
  1117 |     await page.goto("/?show=no_plan");
  1118 |     await expect(page.locator(".tbl-row")).toHaveCount(1);
  1119 |     await expect(page.locator(".tbl-row")).toContainText("#2");
  1120 |     await expect(page).toHaveURL(/show=no_plan/);
  1121 |   });
  1122 | 
  1123 |   test("the request page shows the gap and the example that would fit", async ({
  1124 |     page,
  1125 |   }) => {
  1126 |     await mock(page);
  1127 |     await page.goto("/requests/req_0a7c9d33");
  1128 | 
  1129 |     const section = page
  1130 |       .locator(".block")
  1131 |       .filter({ hasText: "why there is no job" });
  1132 |     // The verdict, in the server's vocabulary: `needs_params` is the thing the
  1133 |     // caller has to close, and it is a word they will see again in the API.
  1134 |     await expect(section.locator(".pill")).toContainText(["needs_params"]);
  1135 |     await expect(section).toContainText("OPRA.PILLAR");
  1136 |     // Every registered type answers, so an absent candidate is not a silence
  1137 |     // a reviewer has to go looking behind.
  1138 |     await expect(section.locator(".candidate").first()).toContainText(
  1139 |       "databento_batch_download",
  1140 |     );
  1141 |     await expect(section.locator(".candidate").first()).toContainText(
  1142 |       "missing symbols, stype_in",
  1143 |     );
  1144 | 
  1145 |     // The closest fit: the caller's own params kept, the gaps as placeholders,
  1146 |     // and the server's own caveat that this is an example, not a quote.
  1147 |     const would = section.locator(".would-fit");
  1148 |     await expect(would).toContainText("the closest fit");
  1149 |     await expect(would).toContainText("OPRA.PILLAR");
  1150 |     await expect(would).toContainText("<symbols>");
  1151 |     await expect(would).toContainText("an example shape, not a quote");
  1152 | 
  1153 |     // No "ask for a new plan" line here: `needs_params` is the caller's own
  1154 |     // gap, the shorter path is theirs to take, and the service refuses the ask
  1155 |     // while a plan merely wants two more fields.
  1156 |     await expect(section).not.toContainText("escalate");
  1157 |     await expect(section).not.toContainText("POST /v1/requests/");
  1158 |   });
  1159 | 
  1160 |   test("a row nobody has asked about names the escalate door; one already asked does not", async ({
```