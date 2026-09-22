// Layout candidates are visual aids, never semantic alignment or stored evidence.
export const bounds = rects => [Math.min(...rects.map(r=>r[0])),Math.min(...rects.map(r=>r[1])),Math.max(...rects.map(r=>r[2])),Math.max(...rects.map(r=>r[3]))];
const area = r => Math.max(0,r[2]-r[0])*Math.max(0,r[3]-r[1]);
const overlap = (a,b) => area([Math.max(a[0],b[0]),Math.max(a[1],b[1]),Math.min(a[2],b[2]),Math.min(a[3],b[3])]);
const compact = text => (text || '').replace(/\s+/gu, '');

export function sentenceBounds(text, start, end, maxLength = 1200) {
  if (typeof text !== 'string' || !Number.isInteger(start) || !Number.isInteger(end)
      || start < 0 || end <= start || end > text.length) return null;
  let selectedStart=start, selectedEnd=end;
  while(selectedStart<selectedEnd && /\s/u.test(text[selectedStart]))selectedStart++;
  while(selectedEnd>selectedStart && /\s/u.test(text[selectedEnd-1]))selectedEnd--;
  if(selectedStart===selectedEnd)return null;
  const segments=[...new Intl.Segmenter(undefined,{granularity:'sentence'}).segment(text)]
    .map(s=>[s.index,s.index+s.segment.length]);
  const matches=segments.filter(([a,b])=>selectedStart<b && selectedEnd>a);
  if(matches.length!==1)return null;
  let [a,b]=matches[0];
  while(a<b && /\s/u.test(text[a]))a++;
  while(b>a && /\s/u.test(text[b-1]))b--;
  return b>a && b-a<=maxLength ? [a,b] : null;
}
// Require coverage in BOTH directions: a word contained in a paragraph is not
// the paragraph's translation anchor. Rectangles may be split into PDF spans.
function coverage(rects, other) {
  const total=rects.reduce((sum,r)=>sum+area(r),0);
  return total>0 ? rects.reduce((sum,r)=>sum+Math.min(area(r),other.reduce((n,a)=>n+overlap(r,a),0)),0)/total : 0;
}
export function covered(selection, anchor) {
  return selection.artifact_id === anchor.artifact_id && selection.artifact_hash === anchor.artifact_hash && selection.page === anchor.page
    && coverage(selection.rects,anchor.rects)>=.9 && coverage(anchor.rects,selection.rects)>=.9
    && (!selection.excerpt || !anchor.excerpt || compact(selection.excerpt)===compact(anchor.excerpt));
}
export function confirmedMatch(selection, notes) {
  const matches=[];
  for(const note of notes.filter(n=>n.confirmed && !n.archived)) for(const link of note.source_links || []) {
    const t=note.anchors[link.translation_index], o=note.anchors[link.original_index];
    if(link.status==='user_confirmed' && t && o && covered(selection,t)) matches.push(o);
  }
  const unique=[...new Map(matches.map(a=>[JSON.stringify([a.artifact_id,a.page,a.rects]),a])).values()];
  return {anchor:unique.length===1 ? unique[0] : null, ambiguous:unique.length>1};
}
export function paragraphs(items) {
  const lines=[];
  for(const item of [...items].sort((a,b)=>b.rect[3]-a.rect[3] || a.rect[0]-b.rect[0])) {
    const r=item.rect,h=r[3]-r[1];
    let line=lines.find(l=>Math.abs(l.rect[3]-r[3])<Math.min(h,l.rect[3]-l.rect[1])*.45
      && r[0]<=l.rect[2]+24 && r[2]>=l.rect[0]-24);
    if(!line){line={rect:r,items:[]};lines.push(line);}
    line.items.push(item);line.rect=bounds(line.items.map(i=>i.rect));
  }
  const blocks=[];
  for(const line of lines.sort((a,b)=>b.rect[3]-a.rect[3] || a.rect[0]-b.rect[0])) {
    const r=line.rect,h=r[3]-r[1];
    const block=blocks.find(b=>{const p=b.lines.at(-1).rect;
      const x=Math.max(0,Math.min(p[2],r[2])-Math.max(p[0],r[0]));
      return x/Math.min(p[2]-p[0],r[2]-r[0])>.5 && p[1]-r[3]>=-h*.2 && p[1]-r[3]<h*1.2;});
    if(block)block.lines.push(line);else blocks.push({lines:[line]});
  }
  return blocks.map(b=>({rect:bounds(b.lines.map(l=>l.rect)),rects:b.lines.map(l=>l.rect),
    excerpt:b.lines.map(l=>l.items.sort((a,b)=>a.rect[0]-b.rect[0]).map(i=>i.text).join('')).join('\n')}));
}
export function selectedParagraph(selection, translated) {
  // Geometric proximity cannot align individual translated words or sentences.
  // Only offer paragraph context when the selection covers that whole block.
  // Browser Range height can include extra font leading (fixture: 77% overlaps
  // the span boxes). Full text equality and reverse coverage prevent widening.
  const matches=translated.filter(b=>coverage(b.rects,selection.rects)>=.95
    && coverage(selection.rects,b.rects)>=(selection.excerpt && b.excerpt ? .7 : .95)
    && (!selection.excerpt || !b.excerpt || compact(selection.excerpt)===compact(b.excerpt)));
  return matches.length===1 ? matches[0] : null;
}
export function layoutCandidate(selection, translated, originals) {
  const block=selectedParagraph(selection,translated);
  if(!block)return null;
  const target=block.rect;
  const ranked=originals.map(b=>{
    const r=b.rect, x=Math.max(0,Math.min(r[2],target[2])-Math.max(r[0],target[0]))/Math.min(r[2]-r[0],target[2]-target[0]);
    const y=Math.abs((r[1]+r[3]-target[1]-target[3])/2);
    return {block:b,score:x*2-y/100,x,y};
  }).filter(r=>r.x>.5 && r.y<Math.max(65,(target[3]-target[1])*.75)).sort((a,b)=>b.score-a.score);
  if(!ranked.length || (ranked[1] && ranked[0].score-ranked[1].score<.2))return null;
  return ranked[0].block;
}
