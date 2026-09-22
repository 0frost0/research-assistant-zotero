"""Local PDF fixtures and validator checks; no model requests."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject, DictionaryObject
from tests.test_reading_memory import pdf
from research_assistant.memory.store import ReadingStore
from research_assistant.translation.references import reference_pages, preserve_reference_pages
from research_assistant.translation.service import TranslationService, validate_output


def entries(start=1):
    return "\n".join(f"{i}. Smith, A.: Research on evidence. Journal of methods (2024)." for i in range(start, start + 4))


def reference_pdf(path, texts):
    base = path.with_name('base.pdf')
    pdf(base)
    writer = PdfWriter()
    for text in texts:
        page = writer.add_page(PdfReader(base).pages[0])
        stream = DecodedStreamObject()
        lines = ["BT /F1 10 Tf 50 730 Td"]
        for line in text.splitlines():
            escaped = line.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')
            lines.append(f"({escaped}) Tj 0 -14 Td")
        stream.set_data(('\n'.join(lines)+' ET').encode('ascii'))
        page[NameObject('/Contents')] = writer._add_object(stream)
    with path.open('wb') as stream: writer.write(stream)


class ReferenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root/'source.pdf'
        reference_pdf(self.source, ['References\n'+entries(), entries(5)])

    def test_heading_and_numbered_continuation(self):
        self.assertEqual(reference_pages(['Body text', 'References\n'+entries(), entries(5)]), [2,3])

    def test_no_blanket_final_page_or_mixed_page_exemption(self):
        for pages in [[entries()], ['Body text '*30+'\nReferences\n'+entries(), entries(5)],
                      ['References\n'+entries(),'Appendix A\n'+entries(5)],
                      ['References\nshort note']]:
            result = reference_pages(pages)
            self.assertNotIn(len(pages), result)

    def test_unchanged_reference_pages_pass_without_chinese(self):
        result = validate_output(self.source, self.source)
        self.assertEqual(result['reference_pages_preserved'],[1,2])
        self.assertEqual(result['human_review'],'pending')

    def test_missing_entries_blank_and_page_count_still_fail(self):
        bad=self.root/'bad.pdf'
        for texts in [['References\n'+entries(),entries(5)[:35]], ['References\n'+entries(),''], ['References\n'+entries()]]:
            reference_pdf(bad,texts)
            with self.assertRaises(ValueError):validate_output(self.source,bad)

    def test_original_pages_restore_layout_and_keep_engine_file(self):
        target=self.root/'engine.pdf'
        reference_pdf(target,['Damaged reference rendering','More damage'])
        before=target.read_bytes()
        fixed,pages=preserve_reference_pages(self.source,target)
        self.assertEqual(pages,[1,2])
        self.assertEqual(target.read_bytes(),before)
        self.assertEqual([p.extract_text() for p in PdfReader(fixed).pages],[p.extract_text() for p in PdfReader(self.source).pages])
        self.assertEqual(validate_output(self.source,fixed)['automated_checks'],'passed')

    def test_recovery_uses_existing_output_without_model_or_new_attempt(self):
        store=ReadingStore(self.root)
        paper=store.register(self.source,'source.pdf')
        job,_=store.reserve_job(paper['id'],{'fixture':True})
        store.update_job(job['id'],state='failed',error='产物未通过完整性检查：参考文献',diagnostics={})
        folder=self.root/'.data/translation_work'/job['id']/str(job['attempt'])
        folder.mkdir(parents=True)
        output=folder/'fixture.no_watermark.zh.mono.pdf'
        reference_pdf(output,['Damaged reference rendering','More damage'])
        service=TranslationService(store)
        with patch('research_assistant.translation.service.configuration',side_effect=AssertionError('No model config needed')):
            restored=service.retry(job['id'])
        self.assertEqual(restored['state'],'succeeded')
        self.assertEqual(restored['attempt'],job['attempt'])
        self.assertIsNotNone(restored['previous_validation_error'])
        self.assertIsNone(service.ledger.summary(job['id']))
        self.assertIsNone(service.recover_validated_output(job['id']))
        self.assertEqual(len(store.paper(paper['id'])['artifacts']),2)

    def test_body_must_still_be_translated(self):
        original=self.root/'body.pdf';pdf(original)
        with self.assertRaises(ValueError):validate_output(original,original)


if __name__=='__main__':unittest.main()
