// playwright-cli run-code --filename scripts/selection_browser_check.js
// Only for scripts/selection_preview.py on 8767: provider is a local fixture.
async (page) => {
  if (!page.url().startsWith("http://127.0.0.1:8767/")) throw Error("Use isolated selection fixture");
  const checks = [];
  const check = (value, name) => { if (!value) throw Error(name); checks.push(name); };
  await page.waitForFunction(() => document.querySelectorAll(".textLayer span").length > 10);
  check(!(await page.locator("#auto").isChecked()), "automatic full PDF translation defaults off");
  const before = await page.evaluate(() => fetch("/api/reading/selection-translation").then(r => r.json()));
  // Use actual rendered PDF text and the same mouseup handler as manual selection.
  await page.evaluate(() => {
    const spans = [...document.querySelectorAll(".textLayer span")].filter(s => s.firstChild?.nodeType === 3 && s.textContent.trim());
    const span = spans.find(s => s.textContent.includes("baseline"));
    if (!span) throw Error("Synthetic baseline sentence missing");
    const start = span.textContent.indexOf("baseline");
    const range = document.createRange();
    range.setStart(span.firstChild, start);
    range.setEnd(span.firstChild, start + "baseline".length);
    getSelection().removeAllRanges(); getSelection().addRange(range);
    document.querySelector(".pdf-page").dispatchEvent(new MouseEvent("mouseup", {bubbles: true}));
  });
  // P-083 tracks lost PDF whitespace separately; this check verifies sentence boundaries.
  await page.waitForFunction(() => document.querySelector("#selection").textContent.replace(/\s+/gu, "").includes("Thebaselineuseslexicalmatching."));
  check(await page.evaluate(() => getSelection().toString().replace(/\s+/gu, "") === "Thebaselineuseslexicalmatching."),
    "partial selection expands to one sentence, ignoring PDF whitespace");
  check((await page.locator("#selection").innerText()).includes("第 1 页"), "source selection captured");
  await page.getByRole("tab", {name:"选区译文",exact:true}).click();
  await page.locator("#translate-selection").click();
  await page.waitForFunction(() => document.querySelector("#selection-translation").textContent.includes("机器译文 · 待核对"));
  const first = await page.evaluate(() => fetch("/api/reading/selection-translation").then(r => r.json()));
  check(first.requests_today === before.requests_today + 1, "one selection makes one mocked request");
  await page.locator("#translate-selection").click();
  await page.waitForFunction(() => document.querySelector("#selection-translation").textContent.includes("本地缓存，本次未调用模型"));
  const second = await page.evaluate(() => fetch("/api/reading/selection-translation").then(r => r.json()));
  check(second.requests_today === first.requests_today, "repeat uses cache without request");
  await page.getByRole("button", {name: "保存为待确认草稿", exact: true}).click();
  await page.waitForFunction(() => document.querySelector("#notice").textContent.includes("译文已保存为 AI 来源草稿"));
  const records = await page.evaluate(async () => {
    const paper = document.querySelector("#papers").value;
    return fetch("/api/reading/notes?paper_id=" + paper).then(r => r.json());
  });
  const draft = records.at(-1);
  check(records.some(n => !n.confirmed && n.generation_source?.kind === "selection_translation"), "AI draft records translation provenance");
  const search = await page.evaluate(() => fetch("/api/reading/search", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({query:"科研证据需要核对"})}).then(r=>r.json()));
  check(search.length === 0, "unconfirmed translation excluded from note retrieval");
  await page.getByRole("tab", {name:"选区译文",exact:true}).click();
  await page.getByRole("button", {name:"返回原文选区", exact:true}).click();
  await page.waitForFunction(() => document.querySelectorAll(".mark.focus").length > 0);
  check(await page.locator(".mark.focus").count() > 0, "translation returns to original selection");
  await page.reload();
  await page.waitForFunction(() => document.querySelectorAll("#translation-history button").length > 0);
  await page.getByRole("tab", {name:"选区译文",exact:true}).click();
  await page.getByText("最近译文（本地保存）", {exact:true}).click();
  await page.locator("#translation-history button").first().click();
  await page.waitForFunction(() => document.querySelector("#selection-translation").textContent.includes("本地缓存"));
  check(true, "history survives browser refresh");
  await page.screenshot({path:"output/playwright/selection-translation.png", fullPage:true});
  console.log(JSON.stringify({fixture:"no real model calls", passed:checks.length, checks}));
}
