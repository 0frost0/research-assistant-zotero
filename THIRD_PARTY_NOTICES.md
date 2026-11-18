# Third-party components used by the reading module

| Component | Pinned version | License / source |
| --- | --- | --- |
| PDFMathTranslate-next | 2.9.0 | AGPL-3.0; https://github.com/PDFMathTranslate-next/PDFMathTranslate-next |
| BabelDOC | 0.6.2 | AGPL-3.0; https://github.com/funstory-ai/BabelDOC |
| PyMuPDF | 1.25.2 | AGPL / commercial licensing; https://pymupdf.readthedocs.io/ |
| PDF.js / pdfjs-dist | 5.4.149 | Apache-2.0; https://github.com/mozilla/pdf.js |
| Lucide (knowledge workspace icons) | 0.468.0 | ISC; https://github.com/lucide-icons/lucide |

These packages are installed by scripts/install_reader.ps1, not copied into the
application source. Preserve their installed LICENSE/NOTICE files in any
distribution. Source and network-use obligations are not waived by process
isolation. See docs/reading/翻译引擎与许可证.md before distributing a combined
package or offering a hosted service. This notice does not relicense the user's
pre-existing project code or replace the full dependency license review.

Lucide's browser bundle is vendored in research_assistant/web/static/vendor.
Its full license is preserved as lucide-LICENSE beside the bundle.

## Evidence-selector training data and model

- **QASPER v0.3**: Pradeep Dasigi, Kyle Lo, Iz Beltagy, Arman Cohan,
  Noah A. Smith, Matt Gardner, *A Dataset of Information-Seeking Questions
  and Answers Anchored in Research Papers*, NAACL 2021,
  https://arxiv.org/abs/2105.03011. Dataset card declares **CC BY 4.0**:
  https://huggingface.co/datasets/allenai/qasper. Paper text originates from
  S2ORC. This project derives incremental evidence-set labels from the public
  human support-paragraph annotations; these are not new human semantic-facet
  annotations. Original archive, attribution card and SHA-256 manifest stay in
  ignored `output/qasper-v03/raw/`. No raw dataset is vendored in source.
- **Qwen/Qwen3-Reranker-0.6B**: Apache-2.0,
  https://huggingface.co/Qwen/Qwen3-Reranker-0.6B, pinned revision
  `e61197ed45024b0ed8a2d74b80b4d909f1255473`. LoRA training creates a separate
  adapter and does not modify the original model files or serving weights.

## SetR protocol adaptation

- SetR IRI prompt is adapted from `LGAI-Research/SetR` at
  `d1ac78b7e41f7c6a2cd8d582934fbba02893f343`, BSD-3-Clause. Copyright
  (c) 2025 the LG AI Research. The prompt in `benchmarks/setr_protocol.py`
  preserves its wording; surrounding parsing, metrics and execution code are
  project adaptations, not the official SetR implementation. Full license:
  `benchmarks/third_party/SetR-LICENSE.md`.
- Official parsed RankZephyr inputs do not contain SetR GPT-4o supervision.
  Underlying MS MARCO / RankZephyr passage rights are separate from the SetR
  code license. Raw data and generated labels are not vendored or redistributed.
- Current substitute teacher candidate: existing shared **Qwen3.5-27B**,
  Apache-2.0, https://huggingface.co/Qwen/Qwen3.5-27B. Local SHA256 manifest
  records actual file identity; no upstream revision is claimed without proof.
  Shared weights are not modified or redistributed. The earlier Qwen3-32B
  download was stopped and is not used. Neither model is official GPT-4o
  supervision. Preserve upstream licensing when distributing any model assets.
