# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.
- Provide a snippet of code with the fix, if possible.

# Test info

- Name: web/e2e/app.spec.js >> a request that is waiting, in three different ways >> a row nobody has asked about names the escalate door; one already asked does not
- Location: web/e2e/app.spec.js:1160:3

# Error details

```
Error: page.goto: Protocol error (Page.navigate): Cannot navigate to invalid URL
Call log:
  - navigating to "/requests/req_0a7c9d33", waiting until "load"

```

# Test source

```ts
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
  1161 |     page,
  1162 |   }) => {
  1163 |     // `needs_params` is never escalated: the caller has a shorter path, and the
  1164 |     // service refuses the ask. So the line that names the door is for the row
  1165 |     // that has no plan at all.
  1166 |     await mock(page, {
  1167 |       detail: {
  1168 |         ...requestDetail,
  1169 |         facet: "no_plan",
  1170 |         facet_label: "No plan yet",
  1171 |         match: {
  1172 |           ...requestDetail.match,
  1173 |           verdict: "not_claimed",
  1174 |           plan_type: null,
  1175 |           candidates: [],
  1176 |           would_fit: undefined,
  1177 |         },
  1178 |       },
  1179 |     });
> 1180 |     await page.goto("/requests/req_0a7c9d33");
       |                ^ Error: page.goto: Protocol error (Page.navigate): Cannot navigate to invalid URL
  1181 |     const section = page
  1182 |       .locator(".block")
  1183 |       .filter({ hasText: "why there is no job" });
  1184 |     await expect(section).toContainText("escalate");
  1185 |     await expect(section).toContainText("scope");
  1186 | 
  1187 |     // Already asked: the row has a date, and the door is not offered twice.
  1188 |     await mock(page, {
  1189 |       detail: {
  1190 |         ...requestDetail,
  1191 |         facet: "requested",
  1192 |         facet_label: "Plan requested",
  1193 |         escalated_at: "2026-11-14T08:15:00Z",
  1194 |         match: { ...requestDetail.match, verdict: "not_claimed" },
  1195 |       },
  1196 |     });
  1197 |     await page.goto("/requests/req_0a7c9d33");
  1198 |     await expect(section).toContainText("new plan asked for");
  1199 |     await expect(section).not.toContainText("POST /v1/requests/");
  1200 |   });
  1201 | });
  1202 | 
```