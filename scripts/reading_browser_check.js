// Run with playwright-cli run-code --filename scripts/reading_browser_check.js.
// Requires scripts/reading_preview.py on 8766. Uses only isolated QA data.
async (page) => {
  const report = [];
  const check = (condition, name, details = {}) => {
    report.push({ name, passed: !!condition, ...details });
    if (!condition) throw new Error(name + ": " + JSON.stringify(details));
  };
  await page.selectOption("#papers", { label: "scientific-en.pdf" });
  await page.waitForFunction(
    () => document.querySelectorAll(".textLayer span").length > 20,
  );
  await page.screenshot({
    path: "output/playwright/reader-original.png",
    fullPage: true,
  });
  async function selectLines(start = 1, end = 3) {
    return page.evaluate(
      ({ start, end }) => {
        const spans = Array.from(
          document.querySelectorAll(".textLayer span"),
        ).filter((s) => s.firstChild?.nodeType === 3 && s.textContent.trim());
        const range = document.createRange();
        range.setStart(spans[start].firstChild, 0);
        range.setEnd(spans[end].firstChild, spans[end].firstChild.length);
        const selection = getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
        document
          .querySelector(".pdf-page")
          .dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
        return selection.toString();
      },
      { start, end },
    );
  }
  const chosen = await selectLines();
  check(chosen.length > 30, "original multiline text selection");
  await page.click("#highlight");
  await page.waitForFunction(() =>
    document.querySelector("#notice").textContent.includes("高亮已保存"),
  );
  const originalMarks = await page.locator(".mark").count();
  check(originalMarks >= 3, "multiline separate rectangles", {
    rectangles: originalMarks,
  });
  await page.fill(
    "#body",
    "我的理解：检索质量影响证据可靠性。我还想验证小样本能否推广。",
  );
  await page.click("#save");
  await page.waitForFunction(() =>
    document.querySelector("#notice").textContent.includes("revision 1"),
  );
  const noteIds = await page.evaluate(async () => {
    const p = document.querySelector("#papers").value;
    return (await (await fetch("/api/reading/notes?paper_id=" + p)).json()).map(
      (n) => n.id,
    );
  });
  check(noteIds.length >= 1, "note saved with source");
  await page.reload();
  await page.selectOption("#papers", { label: "scientific-en.pdf" });
  await page.waitForFunction(
    () => document.querySelectorAll(".mark").length >= 3,
  );
  check(
    (await page.locator(".mark").count()) === originalMarks,
    "refresh restores original marks",
  );
  await page.getByRole("button", { name: "放大", exact: true }).click();
  await page.waitForFunction(
    () => document.querySelector(".pdf-page").style.width === "714px",
  );
  await page.waitForFunction(
    () => document.querySelectorAll(".mark").length >= 3,
  );
  const delta = await page.evaluate(() => {
    const spans = Array.from(
      document.querySelectorAll(".textLayer span"),
    ).filter((s) => s.firstChild?.nodeType === 3 && s.textContent.trim());
    const marks = [...document.querySelectorAll(".mark")].slice(-3);
    return Math.max(
      ...marks.map((m, i) => {
        const r = document.createRange();
        r.selectNodeContents(spans[i + 1]);
        const a = r.getBoundingClientRect(),
          b = m.getBoundingClientRect();
        return Math.max(
          Math.abs(a.left - b.left),
          Math.abs(a.top - b.top),
          Math.abs(a.width - b.width),
          Math.abs(a.height - b.height),
        );
      }),
    );
  });
  check(delta < 2, "zoom: highlight aligns with actual text rectangles", {
    maximum_pixel_delta: delta,
  });
  await page.setViewportSize({ width: 1500, height: 960 });
  check(
    (await page.locator(".mark").count()) === originalMarks,
    "window resize restores marks",
  );
  await page.getByRole("button", { name: "旋转", exact: true }).click();
  await page.waitForFunction(
    () => document.querySelectorAll(".mark").length >= 3,
  );
  await page.screenshot({
    path: "output/playwright/reader-rotated.png",
    fullPage: true,
  });
  check(
    (await page.locator(".mark").count()) === originalMarks,
    "rotation retains marks",
  );
  const versions = await page
    .locator("#versions option")
    .evaluateAll((opts) =>
      opts.map((x) => ({ value: x.value, text: x.textContent })),
    );
  await page.selectOption(
    "#versions",
    versions.find((x) => x.text.includes("中文译文")).value,
  );
  await page.waitForFunction(() =>
    document.querySelector(".textLayer")?.textContent.includes("我们"),
  );
  await selectLines();
  await page.click("#underline");
  await page.waitForFunction(() =>
    document.querySelector("#notice").textContent.includes("下划线已保存"),
  );
  await page.fill(
    "#body",
    "我的疑问：译文表格的列间距变化了，必须核对原文数值。",
  );
  await page.click("#save");
  await page.waitForFunction(() =>
    document.querySelector("#notice").textContent.includes("revision 1"),
  );
  await page.screenshot({
    path: "output/playwright/reader-chinese-note.png",
    fullPage: true,
  });
  const notes = await page.evaluate(
    async () =>
      await (
        await fetch(
          "/api/reading/notes?paper_id=" +
            document.querySelector("#papers").value,
        )
      ).json(),
  );
  const translated = notes.find((n) => n.body.includes("列间距"));
  check(
    translated.anchors[0].kind === "translation" &&
      translated.anchors[0].mapping_status === "unaligned",
    "translation source stays unaligned",
  );
  await page.fill(
    "#body",
    "修正后的理解：译文表格列间距会变化，数值必须核对原文。",
  );
  await page.click("#save");
  await page.waitForFunction(() =>
    document.querySelector("#notice").textContent.includes("revision 2"),
  );
  await page.fill("#query", "译文表格列间距");
  await page.click("#ask");
  await page.waitForFunction(() =>
    document.querySelector("#answer").textContent.includes("revision 2"),
  );
  check(
    (await page.locator("#answer").innerText()).includes("修正后的理解"),
    "Q&A retrieves latest revision offline",
  );
  const sourceLink = await page
    .locator("#answer a")
    .last()
    .getAttribute("href");
  await page.goto("http://127.0.0.1:8766" + sourceLink);
  await page.waitForFunction(
    () => document.querySelectorAll(".mark.focus").length > 0,
  );
  check(
    (await page.locator("#body").inputValue()).includes("修正后的理解"),
    "Q&A citation returns to document selection",
  );
  const other = await page.context().newPage();
  await other.goto(page.url());
  await other.waitForFunction(() =>
    document.querySelector("#body").value.includes("修正后的理解"),
  );
  await page.fill("#body", "窗口一修改：表格间距仍需核对原文。");
  await page.click("#save");
  await page.waitForFunction(() =>
    document.querySelector("#notice").textContent.includes("revision 3"),
  );
  await other.fill("#body", "窗口二未保存的独立想法");
  await other.click("#save");
  await other.waitForFunction(() =>
    document.querySelector("#notice").textContent.includes("输入仍保留"),
  );
  check(
    (await other.locator("#body").inputValue()) === "窗口二未保存的独立想法",
    "two-window conflict preserves unsaved text",
  );
  await other.screenshot({
    path: "output/playwright/reader-conflict.png",
    fullPage: true,
  });
  await other.close({ runBeforeUnload: false });
  await page.goto("http://127.0.0.1:8766/reader");
  await page.selectOption("#papers", { label: "scientific-en.pdf" });
  await page.waitForFunction(
    () => document.querySelectorAll(".textLayer span").length > 20,
  );
  const cross = await page.evaluate(() => {
    const spans = [...document.querySelectorAll(".textLayer span")].filter(
      (s) => s.firstChild?.nodeType === 3 && s.textContent.trim(),
    );
    const left = spans.find((s) => s.textContent.startsWith("We evaluate"));
    const right = spans.find(
      (s) =>
        s.textContent.startsWith("We evaluate") &&
        s.getBoundingClientRect().left >
          left.getBoundingClientRect().left + 100,
    );
    const r = document.createRange();
    r.setStart(left.firstChild, 0);
    r.setEnd(right.firstChild, right.firstChild.length);
    getSelection().removeAllRanges();
    getSelection().addRange(r);
    document
      .querySelector(".pdf-page")
      .dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
    return document.querySelector("#notice").textContent;
  });
  check(
    cross.includes("跨栏"),
    "cross-column selection is explicitly rejected",
  );
  return {
    checks: report,
    note_id: translated.id,
    paper_id: translated.paper_id,
  };
}
