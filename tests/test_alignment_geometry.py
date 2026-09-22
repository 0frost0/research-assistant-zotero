"""Real PDF geometry tests in the isolated alignment runtime; no model calls."""
import tempfile
import unittest
from pathlib import Path
try:
    import pymupdf as fitz
    from research_assistant.translation.alignment_worker import (
        anchor_for,
        continues_same_sentence,
        selected_segments,
        sentences,
    )
except ImportError:
    fitz=None


@unittest.skipIf(fitz is None,'Run in .alignment_env with real PyMuPDF/ONNX dependencies')
class AlignmentGeometryTests(unittest.TestCase):
    @staticmethod
    def segment(text, page, box):
        return {
            'page': page,
            'text': text,
            'chars': [{'c': character, 'box': box} for character in text],
        }

    def test_pdf_block_fragments_merge_only_within_the_same_sentence(self):
        same_line = self.segment('我们在来自两个认', 1, [100, 400, 220, 412])
        same_line_tail = self.segment('知神经病学服务。', 1, [221, 400, 330, 412])
        next_line_tail = self.segment('知神经病学服务。', 1, [100, 387, 230, 399])

        self.assertTrue(continues_same_sentence(same_line, same_line_tail))
        self.assertTrue(continues_same_sentence(same_line, next_line_tail))

        ended = self.segment('This sentence ends.', 1, [100, 400, 220, 412])
        self.assertFalse(continues_same_sentence(ended, same_line_tail))

    def test_pdf_block_fragments_do_not_merge_across_columns_or_pages(self):
        left_column = self.segment('Left column fragment', 1, [50, 400, 250, 412])
        right_column = self.segment('Right column fragment', 1, [330, 387, 550, 399])
        next_page = self.segment('Next page fragment', 2, [50, 387, 250, 399])

        self.assertFalse(continues_same_sentence(left_column, right_column))
        self.assertFalse(continues_same_sentence(left_column, next_page))

    def test_original_coordinates_survive_rotation_and_crop(self):
        with tempfile.TemporaryDirectory() as folder:
            for rotation in (0,90,180,270):
                with self.subTest(rotation=rotation):
                    path=Path(folder)/f'{rotation}.pdf'
                    doc=fitz.open();page=doc.new_page(width=600,height=800)
                    page.insert_text((100,140),'Patients receive treatment.')
                    page.set_cropbox(fitz.Rect(50,50,550,750));page.set_rotation(rotation)
                    doc.save(path);doc.close()
                    segment=next(s for s in sentences(path) if 'Patients' in s['text'])
                    boxes=[c['box'] for c in segment['chars'][:8]]
                    anchor={'rects':boxes,'excerpt':'Patients'}
                    selected=selected_segments([segment],anchor)
                    self.assertEqual(len(selected),1)
                    with fitz.open(path) as loaded:
                        actual=fitz.Rect(boxes[0])*loaded[0].transformation_matrix
                        char=loaded[0].get_text('rawdict')['blocks'][0]['lines'][0]['spans'][0]['chars'][0]
                        self.assertLess(max(abs(a-b) for a,b in zip(actual,char['bbox'])),.01)
                    original={'id':'original','hash':'hash','metadata':{'pages':[{'view_box':[0,0,600,800]}]}}
                    result=anchor_for(segment,[(0,8)],original)
                    self.assertEqual(result['excerpt'],'Patients')
                    self.assertEqual(result['artifact_id'],'original')

    def test_excerpt_mismatch_is_rejected(self):
        segment={'chars':[{'c':'A','box':[0,0,10,10]}]}
        self.assertEqual(selected_segments([segment],{'rects':[[0,0,10,10]],'excerpt':'B'}),[])

    def test_multiline_rectangles_do_not_cover_gap(self):
        original={'id':'o','hash':'h','metadata':{'pages':[{'view_box':[0,0,600,800]}]}}
        segment={'page':1,'text':'AB','chars':[{'c':'A','box':[10,50,20,60]},{'c':'B','box':[300,30,310,40]}]}
        result=anchor_for(segment,[(0,2)],original)
        self.assertEqual(result['rects'],[[10,50,20,60],[300,30,310,40]])
