// Read-only UI check on existing local PDFs. No model or persistence requests.
async page => {
  if(!/^http:\/\/127\.0\.0\.1:876[56]\/reader/.test(page.url()))throw Error('Local reader required');
  await page.route('**/api/**',route=>route.request().method()==='GET' ? route.continue() : route.abort());
  const checks=[];
  const check=(ok,name)=>{if(!ok)throw Error(name);checks.push(name);};
  await page.reload();await page.setViewportSize({width:1440,height:1000});
  const loaded=()=>page.waitForFunction(()=>document.querySelectorAll('.pane').length && [...document.querySelectorAll('.pane')].every(p=>p.getAttribute('aria-busy')==='false'));
  await page.waitForFunction(()=>document.querySelector('#papers').options.length>1);
  if(page.url().includes(':8766/')){
    await page.locator('#papers').selectOption({label:'scientific-en.pdf'});await loaded();
    await page.waitForFunction(()=>document.querySelector('#paper-title').textContent==='scientific-en' && document.querySelector('#versions').options.length>1);
    await page.waitForFunction(()=>document.querySelector('#paper-title').textContent==='scientific-en' && document.querySelector('#versions').options.length>1);
    await page.locator('#versions').selectOption(await page.locator('#versions option').last().getAttribute('value'));await loaded();
  } else await loaded();
  await page.locator('#compare').click();await loaded();
  const select=async count=>page.locator('.pane').last().evaluate((p,count)=>{
    const spans=[...p.querySelectorAll('.textLayer span')].filter(s=>s.firstChild?.nodeType===3 && s.textContent.trim());
    const index=spans.findIndex(s=>/[\u4e00-\u9fff]/u.test(s.textContent));
    if(index<0)throw Error('Chinese text layer required');
    const first=spans[index],start=first.textContent.search(/[\u4e00-\u9fff]/u);
    let endIndex=index,end=start+count;
    while(end>spans[endIndex].firstChild.length){end-=spans[endIndex].firstChild.length;endIndex++;}
    const r=document.createRange();r.setStart(first.firstChild,start);r.setEnd(spans[endIndex].firstChild,end);
    getSelection().removeAllRanges();getSelection().addRange(r);
    p.querySelector('.pdf-page').dispatchEvent(new MouseEvent('mouseup',{bubbles:true}));
  },count);
  const expanded=[];
  for(const count of [1,2]){
    await select(count);
    await page.waitForFunction(n=>getSelection().toString().trim().length>n,count);
    expanded.push(await page.evaluate(()=>getSelection().toString().trim()));
    check(expanded.at(-1).length>count,`${count} character fragment expands to a complete sentence`);
  }
  check(expanded[0]===expanded[1],'fragments in the same sentence normalize to the same anchor');
  await page.locator('.pane').last().getByRole('button',{name:'放大',exact:true}).click();await loaded();
  await select(2);
  await page.waitForFunction(()=>getSelection().toString().trim().length>2);
  check(await page.evaluate(expected=>getSelection().toString().trim()===expected,expanded[1]),'sentence normalization survives zoom');
  await page.screenshot({path:'output/playwright/selection-granularity-'+(page.url().includes(':8766/')?'fixture':'real')+'.png',fullPage:true});
  return {passed:checks.length,checks,model_calls:0,persistence_writes:0,selection_method:'DOM Range'};
}
