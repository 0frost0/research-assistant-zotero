async (page) => {
  const checks = [];
  function check(ok, name, details = {}) {
    checks.push({ name, passed: !!ok, ...details });
    if (!ok) throw Error(name + JSON.stringify(details));
  }
  await page.goto("http://127.0.0.1:8766/reader");
  await page.selectOption("#papers", { label: "scientific-en.pdf" });
  await page.waitForFunction(
    () => document.querySelectorAll("#versions option").length === 2,
  );
  const value = await page
    .locator("#versions option")
    .last()
    .getAttribute("value");
  await page.selectOption("#versions", value);
  await page.waitForFunction(() =>
    document.querySelector(".textLayer")?.textContent.includes("我们"),
  );
  const points = await page.evaluate(() => {
    const box = document.querySelector(".pdf-page").getBoundingClientRect();
    const spans = [...document.querySelectorAll(".textLayer span")].filter(
      (s) => s.firstChild?.nodeType === 3,
    );
    const body = spans
      .map((s) => s.getBoundingClientRect())
      .filter(
        (b) =>
          b.top > box.top + 80 &&
          b.top < box.top + 125 &&
          b.left < box.left + 285,
      );
    const first = body[0],
      last = body[body.length - 1];
    return {
      first: { x: first.left + 1, y: first.top + first.height / 2 },
      last: { x: last.right - 1, y: last.top + last.height / 2 },
    };
  });
  await page.mouse.move(points.first.x, points.first.y);
  await page.mouse.down();
  await page.mouse.move(points.last.x, points.last.y, { steps: 15 });
  await page.mouse.up();
  const selected = await page.locator("#selection").innerText();
  check(selected.length > 50, "native mouse selects multiple Chinese lines", {
    characters: selected.length,
  });
  await page.click("#underline");
  await page.waitForFunction(() =>
    document.querySelector("#notice").textContent.includes("下划线已保存"),
  );
  const before = await page.locator(".mark.underline").count();
  check(before >= 3, "Chinese multiline rectangles stored", {
    rectangles: before,
  });
  await page.getByRole("button", { name: "旋转", exact: true }).click();
  await page.waitForFunction(
    () => document.querySelectorAll(".mark.underline").length >= 3,
  );
  const sides = await page
    .locator(".mark.underline")
    .evaluateAll((ms) =>
      ms.every(
        (m) =>
          m.style.borderLeftWidth === "2px" &&
          m.style.borderBottomWidth === "initial",
      ),
    );
  const actual = await page
    .locator(".mark.underline")
    .first()
    .evaluate((m) => ({
      left: getComputedStyle(m).borderLeftWidth,
      bottom: getComputedStyle(m).borderBottomWidth,
    }));
  check(
    actual.left === "2px" && actual.bottom === "0px",
    "rotated underline follows text baseline",
    actual,
  );
  await page.screenshot({
    path: "output/playwright/reader-chinese-multiline.png",
    fullPage: true,
  });
  await page.reload();
  await page.selectOption("#papers", { label: "scientific-en.pdf" });
  await page.selectOption("#versions", value);
  await page.waitForFunction(
    () => document.querySelectorAll(".mark.underline").length >= 3,
  );
  check(
    (await page.locator(".mark.underline").count()) === before,
    "refresh restores Chinese multiline marks",
  );
  const info = await page.evaluate(async () => {
    const ns = await (
      await fetch(
        "/api/reading/notes?paper_id=" +
          document.querySelector("#papers").value,
      )
    ).json();
    return ns.find((n) => n.revision > 1);
  });
  await page.goto(
    "http://127.0.0.1:8766/reader?paper=" +
      info.paper_id +
      "&note=" +
      info.id +
      "&revision=1",
  );
  await page.waitForFunction(() =>
    document.querySelector("#editing").textContent.includes("历史只读"),
  );
  check(
    await page.locator("#save").isDisabled(),
    "old revision citation opens read-only history",
  );
  await page.goto("http://127.0.0.1:8766/reader");
  await page.selectOption("#papers", { label: "rotated-cropped.pdf" });
  await page.waitForFunction(() =>
    document
      .querySelector(".textLayer")
      ?.textContent.includes("Scientific evidence"),
  );
  await page.evaluate(() => {
    const n = document.querySelector(".textLayer span").firstChild,
      r = document.createRange();
    r.setStart(n, 0);
    r.setEnd(n, 20);
    getSelection().removeAllRanges();
    getSelection().addRange(r);
    document
      .querySelector(".pdf-page")
      .dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
  });
  await page.click("#highlight");
  await page.waitForFunction(() =>
    document.querySelector("#notice").textContent.includes("高亮已保存"),
  );
  const annotation = await page.evaluate(async () => {
    const a = await (
      await fetch(
        "/api/reading/annotations?paper_id=" +
          document.querySelector("#papers").value,
      )
    ).json();
    return a.at(-1);
  });
  check(
    annotation.anchor.rotation === 90 &&
      annotation.anchor.user_unit === 2 &&
      annotation.anchor.view_box[0] === 30,
    "intrinsic rotation crop offset and UserUnit preserved",
  );
  const geom = await page.evaluate(() => {
    const s = document.querySelector(".textLayer span").firstChild,
      r = document.createRange();
    r.setStart(s, 0);
    r.setEnd(s, 20);
    const a = r.getBoundingClientRect(),
      b = document.querySelector(".mark").getBoundingClientRect();
    return Math.max(
      Math.abs(a.left - b.left),
      Math.abs(a.top - b.top),
      Math.abs(a.width - b.width),
      Math.abs(a.height - b.height),
    );
  });
  check(geom < 2, "rotated cropped highlight matches selected glyphs", {
    maximum_pixel_delta: geom,
  });
  await page.setViewportSize({ width: 1100, height: 850 });
  await page.reload();
  await page.waitForFunction(
    () => document.querySelectorAll(".mark").length > 0,
  );
  await page.screenshot({
    path: "output/playwright/reader-crop-userunit.png",
    fullPage: true,
  });
  return checks;
}
