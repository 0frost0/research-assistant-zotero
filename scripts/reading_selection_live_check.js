// Read-only real artifact check. No API POSTs or model calls.
async page => {
  if(!page.url().startsWith('http://127.0.0.1:8765/reader?paper=044233aa-f4fc-4c6d-8f04-09807c621e69'))throw Error('Expected existing paper');
  await page.route('**/api/**',route=>route.request().method()==='GET' ? route.continue() : route.abort());
  await page.reload();await page.setViewportSize({width:1600,height:1100});
  await page.waitForFunction(()=>document.querySelector('.pane')?.getAttribute('aria-busy')==='false' && document.querySelector('#versions').value==='30aece23-fcd6-4ac8-acd8-59405cc7ed9b');
  await page.locator('.pane').last().evaluate(p=>{
    const spans=[...p.querySelectorAll('.textLayer span')].filter(s=>s.firstChild?.nodeType===3 && s.textContent.trim());
    let start=spans.findIndex(s=>(s.textContent.match(/[\u4e00-\u9fff]/g)||[]).length>=15);
    if(start<0){
      const joined=spans.map(s=>s.textContent).join('');
      const offset=joined.indexOf('摘要');let used=0;
      start=offset<0 ? -1 : spans.findIndex(s=>{used+=s.textContent.length;return used>offset;});
    }
    if(start<0)throw Error('No paragraph found');
    const first=spans[start],last=spans[Math.min(start+(first.textContent.length>15 ? 2 : 50),spans.length-1)];
    const r=document.createRange();r.setStart(first.firstChild,0);r.setEnd(last.firstChild,last.firstChild.length);
    getSelection().removeAllRanges();getSelection().addRange(r);p.querySelector('.pdf-page').dispatchEvent(new MouseEvent('mouseup',{bubbles:true}));
  });
  await page.waitForFunction(()=>document.querySelector('#selection-sync-status').textContent && !document.querySelector('#selection-sync-status').textContent.includes('正在定位'));
  const status=await page.locator('#selection-sync-status').innerText();
  const marks=await page.locator('.pane').first().locator('.sync-candidate').count();
  await page.screenshot({path:'output/playwright/selection-sync-real.png',fullPage:true});
  return {status,original_highlight_lines:marks,model_calls:0,persistence_writes:0};
}
