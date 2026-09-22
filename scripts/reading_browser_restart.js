async page => {
  const report=[];
  const check=(ok,name)=>{report.push({name,passed:!!ok});if(!ok)throw Error(name);};
  await page.goto('http://127.0.0.1:8766/reader');
  await page.selectOption('#papers',{label:'scientific-en.pdf'});
  await page.waitForFunction(()=>document.querySelectorAll('.textLayer span').length>20);
  check(await page.locator('.mark').count()>=3,'original marks survive server restart');
  const notes=await page.evaluate(async()=>await(await fetch('/api/reading/notes?paper_id='+document.querySelector('#papers').value)).json());
  const translated=notes.find(n=>n.revision===3);check(!!translated,'latest note revision survives restart');
  await page.goto('http://127.0.0.1:8766/reader?paper='+translated.paper_id+'&note='+translated.id);
  await page.waitForFunction(()=>document.querySelectorAll('.mark.focus').length>0);
  check((await page.locator('#editing').innerText()).includes('revision 3'),'note restores translated artifact and selection');
  // Add original selection as a second, independent anchor to a translation note.
  const original=await page.locator('#versions option').first().getAttribute('value');await page.selectOption('#versions',original);
  await page.waitForFunction(()=>document.querySelector('.textLayer')?.textContent.includes('We evaluate'));
  await page.evaluate(()=>{const s=[...document.querySelectorAll('.textLayer span')].find(s=>s.textContent.startsWith('We evaluate')).firstChild;const r=document.createRange();r.selectNodeContents(s);getSelection().removeAllRanges();getSelection().addRange(r);document.querySelector('.pdf-page').dispatchEvent(new MouseEvent('mouseup',{bubbles:true}));});
  await page.click('#attach');await page.click('#save');await page.waitForFunction(()=>document.querySelector('#notice').textContent.includes('revision 4'));
  const updated=await page.evaluate(async id=>await(await fetch('/api/reading/notes/'+id)).json(),translated.id);
  check(updated.anchors.some(a=>a.kind==='original')&&updated.anchors.some(a=>a.kind==='translation'&&a.mapping_status==='unaligned'),'manual original anchor does not fabricate translation alignment');
  // Cross-column rejection uses actual rendered spans after glyph merging.
  await page.selectOption('#versions',original);await page.waitForFunction(()=>document.querySelector('.textLayer')?.textContent.includes('We evaluate'));
  const cross=await page.evaluate(()=>{const spans=[...document.querySelectorAll('.textLayer span')].filter(s=>s.firstChild?.nodeType===3);const left=spans.find(s=>s.textContent.startsWith('We evaluate'));const right=spans.find(s=>s.textContent.startsWith('We evaluate')&&s.getBoundingClientRect().left>left.getBoundingClientRect().left+100);const r=document.createRange();r.setStart(left.firstChild,0);r.setEnd(right.firstChild,right.firstChild.length);getSelection().removeAllRanges();getSelection().addRange(r);document.querySelector('.pdf-page').dispatchEvent(new MouseEvent('mouseup',{bubbles:true}));return document.querySelector('#notice').textContent;});
  check(cross.includes('跨栏'),'cross-column rejection remains effective after Chinese fix');
  await page.click('#highlight');check((await page.locator('#notice').innerText()).includes('先选择文字'),'invalid selection cannot save stale previous highlight');
  await page.click('#archive');await page.waitForFunction(()=>document.querySelector('#notice').textContent.includes('revision 5'));
  await page.fill('#query','表格间距');await page.click('#ask');await page.waitForFunction(()=>!document.querySelector('#ask').disabled);
  check(!(await page.locator('#answer').innerText()).includes(translated.id),'archived note excluded from default Q&A');
  await page.goto('http://127.0.0.1:8766/reader?paper='+translated.paper_id+'&note='+translated.id+'&revision=1');
  await page.waitForFunction(()=>document.querySelector('#editing').textContent.includes('历史只读'));
  await page.waitForFunction(()=>document.querySelectorAll('.mark.focus').length>0);
  check(await page.locator('.mark.focus').count()>0,'archived note history still returns to old translation');
  await page.screenshot({path:'output/playwright/reader-restart-history.png',fullPage:true});
  return report;
}
