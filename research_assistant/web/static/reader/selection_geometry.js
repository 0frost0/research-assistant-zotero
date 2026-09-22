// PDF coordinates, not CSS coordinates. Merge neighboring glyphs first: a
// Chinese PDF may have one span per character, which is not a separate column.
export function selectionLines(rects) {
  const rows = [];
  for (const rect of [...rects].sort((a, b) => b[3] - a[3] || a[0] - b[0])) {
    let row = rows.find(
      (r) =>
        Math.min(r.top, rect[3]) - Math.max(r.bottom, rect[1]) >
        Math.min(r.top - r.bottom, rect[3] - rect[1]) * 0.45,
    );
    if (!row) {
      row = { top: rect[3], bottom: rect[1], rects: [] };
      rows.push(row);
    }
    row.rects.push(rect);
  }
  const lines = [];
  for (const row of rows) {
    const spans = row.rects.sort((a, b) => a[0] - b[0]);
    let merged = [...spans[0]];
    for (const rect of spans.slice(1)) {
      if (rect[0] - merged[2] > Math.max(20, (row.top - row.bottom) * 1.6)) {
        throw Error("检测到跨栏或不连续选区，请分别划线，避免覆盖无关内容。");
      }
      merged = [
        Math.min(merged[0], rect[0]),
        Math.min(merged[1], rect[1]),
        Math.max(merged[2], rect[2]),
        Math.max(merged[3], rect[3]),
      ];
    }
    lines.push(merged);
  }
  return lines;
}
