// Existing PDF + real LOCAL model. Allows alignment only; no note/translation writes.
async page => {
  if(!/^http:\/\/127\.0\.0\.1:876[57]\/reader\?paper=/.test(page.url()))throw Error('Verified local SSH tunnel required');
  await page.route('**/api/**',route=>{
    const request=route.request();
    return request.method()==='GET' || request.url().endsWith('/api/reading/alignment') ? route.continue() : route.abort();
  });
  const checks=[];const check=(ok,name)=>{if(!ok)throw Error(name);checks.push(name);};
  await page.reload();await page.setViewportSize({width:1600,height:1100});
  const loaded=()=>page.waitForFunction(()=>document.querySelectorAll('.pane').length && [...document.querySelectorAll('.pane')].every(p=>p.getAttribute('aria-busy')==='false'));
  await loaded();await page.locator('#compare').click();await loaded();
  const selectionPoints=async()=>{
    const translated=page.locator('.pane').last();
    await translated.evaluate(p=>{
      const spans=[...p.querySelectorAll('.textLayer span')].filter(s=>s.firstChild?.nodeType===3);
      const text=spans.map(s=>s.textContent).join(''),offset=text.indexOf('患者');
      if(offset<0)throw Error('Expected existing Chinese phrase');
      let consumed=0;
      for(const span of spans){
        if(consumed+span.textContent.length>offset){span.scrollIntoView({block:'center'});return;}
        consumed+=span.textContent.length;
      }
    });
    await page.waitForTimeout(50);
    return translated.evaluate(p=>{
    const spans=[...p.querySelectorAll('.textLayer span')].filter(s=>s.firstChild?.nodeType===3);
    const text=spans.map(s=>s.textContent).join(''),offset=text.indexOf('患者');
    if(offset<0)throw Error('Expected existing Chinese phrase');
      const locate=pos=>{for(const s of spans){if(pos<s.firstChild.length)return[s.firstChild,pos];pos-=s.firstChild.length;}throw Error('Text offset out of range');};
      const box=(node,start,end)=>{const r=document.createRange();r.setStart(node,start);r.setEnd(node,end);return r.getBoundingClientRect();};
      const a=locate(offset),b=locate(offset+1),ar=box(a[0],a[1],a[1]+1),br=box(b[0],b[1],b[1]+1);
      return {start:{x:ar.left+1,y:ar.top+ar.height/2},end:{x:br.right-1,y:br.top+br.height/2}};
    });
  };
  const responsePromise=page.waitForResponse(r=>r.url().endsWith('/api/reading/alignment') && r.request().method()==='POST',{timeout:90000});
  const points=await selectionPoints();
  await page.mouse.move(points.start.x,points.start.y);await page.mouse.down();
  await page.mouse.move(points.end.x,points.end.y);await page.mouse.up();await page.waitForTimeout(100);
  await page.waitForFunction(()=>document.querySelector('.sync-picked, .sync-confirmed'),null,{timeout:5000});
  check(await page.locator('.pane').last().locator('.sync-picked, .sync-confirmed').count()>0,
    'translated full sentence is highlighted immediately');
  const alignmentResponse=await responsePromise;
  const alignmentRequest=alignmentResponse.request().postDataJSON();
  const result=await alignmentResponse.json();
  console.log('sentence-alignment-result',JSON.stringify({status:result.status,message:result.message,
    alignment_unit:result.alignment_unit,anchor_count:result.anchors?.length || 0,metrics:result.metrics || []}));
  check(result.status==='aligned' && result.alignment_unit==='sentence',
    'real local model returns sentence alignment: '+JSON.stringify({status:result.status,message:result.message,anchor:alignmentRequest.anchor}));
  check(result.anchors.length===1 && result.anchors[0].excerpt.toLowerCase().includes('patients')
    && result.anchors[0].excerpt.length>20,'Chinese sentence maps to one complete English sentence');
  check(result.confirmed===false && result.local_only===true,'machine source remains unconfirmed and local');
  await page.waitForFunction(()=>document.querySelector('.sync-machine'));
  const total=await page.locator('.pane').first().locator('.sync-machine').evaluateAll(m=>m.reduce((sum,n)=>sum+n.getBoundingClientRect().width,0));
  check(total>100,'original highlight covers a complete sentence, not one word');
  await page.locator('.pane').first().getByRole('button',{name:'放大',exact:true}).click();await loaded();
  await page.locator('.pane').first().locator('.sync-machine').first().waitFor({state:'visible',timeout:10000});
  check(await page.locator('.sync-machine').count()>0,'machine highlight survives zoom');
  await page.screenshot({path:'output/playwright/semantic-alignment-real.png',fullPage:true});
  await page.locator('#sync-selection').uncheck();
  check(await page.locator('.sync-machine').count()===0,'disabling clears machine highlights');
  await page.locator('#sync-selection').check();
  // Repeat the identical anchor: zoom can change floating point DOM rectangles.
  const cached=await page.evaluate(async body=>(await fetch('/api/reading/alignment',{
    method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)
  })).json(),alignmentRequest);
  check(cached.cached===true,'repeat selection reuses persistent cache');
  return{passed:checks.length,checks,paid_calls:0,selection_method:'real mouse drag'};
}
