// Browser UI + actual local persistence; synthetic PDFs, no model calls.
async (page) => {
  if (!page.url().startsWith("http://127.0.0.1:8766/")) throw Error("Isolated preview required");
  const cfg = await page.evaluate(() => fetch("/api/reading/settings").then(r => r.json()));
  if (cfg.model !== "fixture-no-network") throw Error("Mock provider required");
  const checks = [];
  const check = (ok, name) => { if (!ok) throw Error(name); checks.push(name); };
  const loaded = () => page.waitForFunction(() => document.querySelectorAll(".pane").length && [...document.querySelectorAll(".pane")].every(p => p.getAttribute("aria-busy") === "false"));
  await page.reload();
  await page.setViewportSize({width:1440,height:1000});
  await page.waitForFunction(() => [...document.querySelector("#papers").options].some(o => o.textContent === "scientific-en.pdf"));
  await page.locator("#papers").selectOption({label:"scientific-en.pdf"}); await loaded();
  await page.locator("#sync-selection").uncheck();
  const originalId = await page.locator("#versions").inputValue();
  const translationId = await page.locator("#versions option").last().getAttribute("value");
  await page.locator("#versions").selectOption(translationId); await loaded();
  check(await page.locator(".pane canvas").evaluate(c => c.width / c.getBoundingClientRect().width >= 1.99), "canvas density >=2 at DPR1");
  await page.locator("#new").click();
  const select = async (paneIndex) => page.locator(".pane").nth(paneIndex).evaluate(p => {
    const spans = [...p.querySelectorAll(".textLayer span")].filter(s => s.firstChild?.nodeType===3 && s.textContent.trim());
    const r = document.createRange(); r.setStart(spans[1].firstChild,0); r.setEnd(spans[2].firstChild,spans[2].firstChild.length);
    getSelection().removeAllRanges(); getSelection().addRange(r);
    p.querySelector(".pdf-page").dispatchEvent(new MouseEvent("mouseup",{bubbles:true}));
  });
  await select(0); await page.locator("#attach").click();
  const noteText = "中文译文手动追溯测试 " + Date.now();
  await page.locator("#body").fill(noteText);
  await page.getByRole("button",{name:"补选对应原文句",exact:true}).click(); await loaded();
  check(!(await page.locator("#sync-pages").isChecked()), "link workflow does not assume page alignment");
  await select(0);
  // Confirmation is mocked only on the verified isolated provider.
  await page.evaluate(() => { window.confirm = () => true; });
  await page.getByRole("button",{name:"确认这两段对应",exact:true}).click();
  await page.locator("#save").click();
  await page.waitForFunction(() => document.querySelector("#notice").textContent.includes("原子保存")); await loaded();
  const getNote = () => page.evaluate(async text => {
    const paper = document.querySelector("#papers").value;
    return (await fetch("/api/reading/notes?paper_id="+paper).then(r=>r.json())).find(n=>n.body===text);
  }, noteText);
  let note = await getNote();
  check(note.source_links[0].status === "user_confirmed" && note.source_links[0].confirmed_by === "user", "manual confirmation persisted");
  check(note.anchors[0].artifact_id === translationId && note.anchors[1].artifact_id === originalId, "independent original and translation artifacts");
  check(note.anchors[0].mapping_status === "unaligned", "manual link does not forge engine alignment");
  await page.getByRole("button",{name:"返回对应原文",exact:true}).click(); await loaded();
  check(await page.locator(".pane").first().locator(".focus").count() > 0, "original selection focused on return");
  await page.locator(".pane").first().getByRole("button",{name:"150%阅读",exact:true}).click(); await loaded();
  check((await page.locator(".zoom-label").first().innerText()) === "150%", "readable zoom control");
  await page.screenshot({path:"output/playwright/reader-sharp-linked.png",fullPage:true});
  await page.reload();
  await page.waitForFunction(() => [...document.querySelector("#papers").options].some(o => o.textContent === "scientific-en.pdf"));
  await page.locator("#papers").selectOption({label:"scientific-en.pdf"}); await loaded();
  await page.locator(".note").filter({hasText:noteText}).getByRole("button",{name:"打开 / 编辑",exact:true}).click(); await loaded();
  await page.getByRole("button",{name:"返回对应原文",exact:true}).waitFor({state:"visible"});
  check(await page.getByRole("button",{name:"返回对应原文",exact:true}).count() === 1, "link survives browser reload");
  await page.locator("#body").fill(noteText + " 修改"); await page.locator("#save").click();
  await page.waitForFunction(() => document.querySelector("#notice").textContent.includes("revision 2"));
  const history = await page.evaluate(id => fetch("/api/reading/history/"+id).then(r=>r.json()),note.id);
  check(history.length === 2 && history.every(n=>n.source_links[0].confirmed_at===note.source_links[0].confirmed_at), "revision retains confirmed pair and timestamp");
  await loaded();
  await page.locator("#sync-selection").check();
  await select(0);
  await page.waitForFunction(()=>document.querySelector("#selection-sync-status").textContent.includes("你确认过"));
  check(await page.locator(".pane").first().locator(".sync-confirmed").count()>0, "selection prefers persisted user-confirmed original pair");
  return {passed:checks.length, checks, model_calls:0, note_id:note.id};
}
