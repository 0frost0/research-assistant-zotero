"""Conservative numbered bibliography detection; mixed pages stay strict."""
import re
from pathlib import Path
from pypdf import PdfReader, PdfWriter


def reference_pages(texts):
    result, previous = [], None
    for index, text in enumerate(texts):
        heading = re.search(r"(?im)^\s*(?:\d+\.?\s+)?(?:references|bibliography|参考文献)\s*$", text)
        if re.search(r"(?im)^\s*(?:[A-Z\d]+[. ]+)?(?:appendix|appendices|supplementary|conclusion|discussion)\b", text):
            previous = None
            continue
        start = heading.end() if heading else 0
        entries = list(re.finditer(r"(?m)^\s*(?:\[(\d{1,3})\]|(\d{1,3})\.)\s+[A-Z]", text[start:]))
        numbers = [int(e[1] or e[2]) for e in entries]
        continuous = bool(numbers) and numbers == list(range(numbers[0], numbers[0] + len(numbers)))
        begins = (heading and len(text[:heading.start()].strip()) < 120
                  or not heading and previous is not None and numbers and numbers[0] == previous + 1)
        years = len(re.findall(r"\b(?:19|20)\d{2}\b", text[start:]))
        if begins and entries and entries[0].start() < 120 and continuous and len(numbers) >= 3 and years >= 3:
            result.append(index + 1)
            previous = numbers[-1]
        else:
            previous = None
    return result


def preserve_reference_pages(original_path, output_path):
    """Retain the original and engine output, write a separate derived PDF."""
    source, target = PdfReader(original_path), PdfReader(output_path)
    if len(source.pages) != len(target.pages):
        return Path(output_path), []
    pages = reference_pages([p.extract_text() or "" for p in source.pages])
    if not pages:
        return Path(output_path), []
    writer = PdfWriter()
    for i, page in enumerate(target.pages, 1):
        writer.add_page(source.pages[i - 1] if i in pages else page)
    result = Path(output_path).with_name(Path(output_path).stem + ".references-preserved.pdf")
    with result.open("wb") as stream:
        writer.write(stream)
    return result, pages
