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
