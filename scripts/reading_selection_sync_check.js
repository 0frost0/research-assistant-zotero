// Synthetic local PDF browser check, no model calls or production writes.
async page => {
  if(!page.url().startsWith('http://127.0.0.1:8766/'))throw Error('Preview required');
  const checks=[];const check=(ok,name)=>{if(!ok)throw Error(name);checks.push(name);};
  const loaded=()=>page.waitForFunction(()=>document.querySelectorAll('.pane').length && [...document.querySelectorAll('.pane')].every(p=>p.getAttribute('aria-busy')==='false'));
  await page.reload();await page.setViewportSize({width:1440,height:1000});
  await page.waitForFunction(()=>[...document.querySelector('#papers').options].some(o=>o.textContent==='scientific-en.pdf'));
  await page.locator('#papers').selectOption({label:'scientific-en.pdf'});await loaded();
  const readNotes=()=>page.evaluate(()=>fetch('/api/reading/notes?paper_id='+document.querySelector('#papers').value).then(r=>r.json()));
  const initialNotes=await readNotes();
  const translated=await page.locator('#versions option').last().getAttribute('value');
  await page.locator('#versions').selectOption(translated);await loaded();
  const select=()=>page.locator('.pane').last().evaluate(async p=>{
    const {paragraphs}=await import('/static/reader/selection_sync.js');
    const spans=[...p.querySelectorAll('.textLayer span')].filter(s=>s.firstChild?.nodeType===3 && s.textContent.trim());
    const scale=Number(p.querySelector('.pdf-page').style.getPropertyValue('--scale-factor'));
    const items=spans.map(s=>{const r=s.getBoundingClientRect();return{text:s.textContent,rect:[r.left/scale,-r.bottom/scale,r.right/scale,-r.top/scale]};});
    const block=paragraphs(items).find(b=>b.rects.length>1);
    if(!block)throw Error('Expected a multi-line paragraph');
    const inside=items.map((item,i)=>({item,i})).filter(({item})=>item.rect[0]>=block.rect[0]-.1 && item.rect[2]<=block.rect[2]+.1 && item.rect[1]>=block.rect[1]-.1 && item.rect[3]<=block.rect[3]+.1);
    const first=spans[inside[0].i],last=spans[inside.at(-1).i];
    const r=document.createRange();r.setStart(first.firstChild,0);r.setEnd(last.firstChild,last.firstChild.length);
    getSelection().removeAllRanges();getSelection().addRange(r);
    p.querySelector('.pdf-page').dispatchEvent(new MouseEvent('mouseup',{bubbles:true}));
  });
  await select();
  await page.waitForFunction(()=>document.querySelector('#selection-sync-status').textContent.includes('候选（橙色）'));
  check(await page.locator('.pane').count()===2,'selecting translation opens original side');
  check(await page.locator('.pane').first().locator('.sync-candidate').count()>0,'layout candidate highlighted in original');
  check(await page.locator('.pane').last().locator('.sync-confirmed').count()>0,'translation selection preserved visually');
  await page.screenshot({path:'output/playwright/selection-sync-candidate.png',fullPage:true});
  const before=await page.locator('.pane').first().evaluate(p=>{const m=p.querySelector('.sync-candidate').getBoundingClientRect(),b=p.querySelector('.pdf-page').getBoundingClientRect();return[(m.x-b.x)/b.width,(m.y-b.y)/b.height];});
  await page.locator('.pane').first().getByRole('button',{name:'放大',exact:true}).click();await loaded();
  const after=await page.locator('.pane').first().evaluate(p=>{const m=p.querySelector('.sync-candidate').getBoundingClientRect(),b=p.querySelector('.pdf-page').getBoundingClientRect();return[(m.x-b.x)/b.width,(m.y-b.y)/b.height];});
  check(before.every((v,i)=>Math.abs(v-after[i])<.003),'highlight remains anchored after zoom');
  const notes=await readNotes();
  check(JSON.stringify(notes)===JSON.stringify(initialNotes),'candidate does not create or change a note');
  await page.locator('#sync-selection').uncheck();
  check(await page.locator('.sync-candidate,.sync-confirmed').count()===0,'disabling clears temporary highlights');
  await select();await page.waitForTimeout(300);
  check(await page.locator('.sync-candidate,.sync-confirmed').count()===0,'disabled mode does not match');
  await page.locator('#sync-selection').check();await select();
  await page.waitForFunction(()=>document.querySelector('.sync-candidate'));
  await page.locator('.pane').last().getByRole('button',{name:'下一页',exact:true}).click();await loaded();
  check(await page.locator('.sync-candidate,.sync-confirmed').count()===0,'page navigation clears stale highlight');
  return {passed:checks.length,checks,model_calls:0};
}
