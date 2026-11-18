"""Real filesystem regression for Windows deep cache paths and containment."""
import json
from pathlib import Path
import tempfile
import unittest

from research_assistant.ingestion.mineru_extractor import MinerUExtractor, MinerUExtractionError, _filesystem_path


class MinerULongPathTests(unittest.TestCase):
    def test_deep_cache_image_is_readable_and_escape_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = _filesystem_path(Path(temp))
            paper = root / "paper.pdf"
            paper.write_bytes(b"fixture")
            cache = root / ("nested-" * 12) / ("nested-" * 12)
            extractor = MinerUExtractor(cache_dir=cache)
            home = extractor.cache_path(paper)
            parsed = home / "paper" / "vlm"
            images = parsed / "images"
            images.mkdir(parents=True)
            image = images / ("a" * 64 + ".jpg")
            image.write_bytes(b"fixture-image")
            self.assertGreater(len(str(image)), 260)
            (home / "manifest.json").write_text("{}", encoding="utf-8")
            content = parsed / "paper_content_list.json"
            content.write_text(json.dumps([{"type": "image", "page_idx": 0,
                "img_path": "images/" + image.name, "image_caption": ["fixture caption"]}]), encoding="utf-8")
            docs, _ = extractor.load_cached(paper)
            self.assertEqual(Path(docs[0].metadata["image_path"]).read_bytes(), b"fixture-image")
            outside = root / "outside.jpg"
            outside.write_bytes(b"outside")
            content.write_text(json.dumps([{"type": "image", "page_idx": 0, "img_path": str(outside)}]), encoding="utf-8")
            with self.assertRaises(MinerUExtractionError):
                extractor.load_cached(paper)
            # Cleanup uses the extended prefix on Windows as well.
            import shutil
            shutil.rmtree(cache)


if __name__ == "__main__":
    unittest.main()
