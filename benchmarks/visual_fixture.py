"""Synthetic visual contracts only; no network, learned embeddings or quality gold."""
import base64
import hashlib
import io
import json

from PIL import Image, ImageDraw


CAPTION = "Synthetic two-bar chart. Compare the blue and orange bars."


def chart_png(blue_taller=True):
    image = Image.new("RGB", (240, 180), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((35, 30 if blue_taller else 100, 95, 155), fill="blue")
    draw.rectangle((145, 100 if blue_taller else 30, 205, 155), fill="orange")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def write_visual_pdf(path):
    """Put the same chart in the fixture PDF, so the source jump is truthful."""
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import DecodedStreamObject, NameObject
    from tests.test_reading_memory import pdf
    pdf(path)
    reader = PdfReader(io.BytesIO(path.read_bytes()))
    page = reader.pages[0]
    content = page.get_contents().get_data()
    stream = DecodedStreamObject()
    stream.set_data(content + b"\nq 1 0 0 1 60 400 cm 0 0 1 rg 35 25 61 126 re f 1 0.6470588 0 rg 145 25 61 56 re f Q\n")
    page[NameObject("/Contents")] = stream
    writer = PdfWriter()
    writer.add_page(page)
    with path.open("wb") as output:
        writer.write(output)


class BoundedVisualCaller:
    """Only visual calls can reach the provider; one native run, two calls total."""
    def __init__(self, delegate, report_path):
        self.delegate, self.report_path = delegate, report_path
        self.report = {"purpose": "native_synthetic_visual_acceptance", "max_calls": 2,
                       "model_calls": 0, "unknown_usage_calls": 0,
                       "input_tokens": 0, "output_tokens": 0, "calls": []}
        self.save()

    def save(self):
        self.report_path.write_text(json.dumps(self.report, indent=2), encoding="utf-8")

    async def __call__(self, role, payload, timeout):
        if (payload.get("multimodal") is not True or role not in {"answer", "review"}
                or not payload.get("_visual_inputs") or self.report["model_calls"] >= 2):
            raise ValueError("native_visual_call_budget_or_scope")
        self.report["model_calls"] += 1
        self.report["unknown_usage_calls"] += 1
        call = {"role": role, "status": "started", "images": len(payload["_visual_inputs"])}
        self.report["calls"].append(call)
        self.save()
        try:
            value, usage = await self.delegate(role, payload, timeout)
            if usage and all(type(usage.get(k)) is int for k in ("input_tokens", "output_tokens")):
                self.report["unknown_usage_calls"] -= 1
                for key in ("input_tokens", "output_tokens"):
                    self.report[key] += usage[key]
            call["status"] = "returned" if value is not None else "invalid_output"
            return value, usage
        except BaseException as exc:
            call.update(status="failed", error_type=type(exc).__name__)
            raise
        finally:
            self.save()


def taller_color(data_url):
    """Pixel-reading test double, not a visual model."""
    with Image.open(io.BytesIO(base64.b64decode(data_url.split(",", 1)[1]))) as image:
        colors = dict((color, count) for count, color in image.convert("RGB").getcolors(240 * 180))
        blue = colors.get((0, 0, 255), 0)
        orange = colors.get((255, 165, 0), 0)
    if not blue or not orange or blue == orange:
        raise ValueError("Invalid synthetic chart")
    return "blue" if blue > orange else "orange"


def seed_visual_report(store, sha):
    """Attach a labelled synthetic parser crop to the already stored fixture PDF."""
    path = store.home / "mineru" / sha / "fixture-bars.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(chart_png())
    with store.transaction() as con:
        row = con.execute("SELECT pages,report FROM objects WHERE hash=?", (sha,)).fetchone()
        report = json.loads(row["report"])
        report["mineru"] = {"blocks": [
            {"text": page["text"], "metadata": {"page": page["page"], "content_type": "text"}}
            for page in json.loads(row["pages"])
        ] + [{"text": CAPTION, "metadata": {"page": 1, "content_type": "image",
                "bbox": [35, 30, 205, 155], "image_path": str(path)}}]}
        con.execute("UPDATE objects SET report=? WHERE hash=?", (json.dumps(report), sha))


def visual_service(store):
    """Real local Qdrant/evidence materialization, deliberately fake embeddings."""
    from qdrant_client import QdrantClient
    from research_assistant.knowledge.service import KnowledgeService
    from research_assistant.retrieval.unified_index import UnifiedMultimodalIndex

    class Embeddings:
        model_name, dimension = "synthetic-native-visual-contract", 2

        def embed_texts(self, texts):
            return [[1., 0.] for _ in texts]

        def embed_images(self, paths, captions=None):
            return [[1., 0.] for _ in paths]

        def embed_query(self, query):
            return [1., 0.]

    class FixtureService(KnowledgeService):
        def _build_index(self, allowed_sources, scope):
            index = super()._build_index(allowed_sources, scope)
            if index is not None and any(chunk.metadata.get("image_path") for chunk in index.chunks):
                client = QdrantClient(":memory:")
                index.unified_index = UnifiedMultimodalIndex(embeddings=Embeddings(), client=client,
                    evidence_path=store.home / "fixture-evidence" / (hashlib.sha256(scope.encode()).hexdigest() + ".sqlite3"))
                index.unified_index.sync(index.chunks)
                search = index.search_results

                def search_with_pixels(query, **kwargs):
                    hits = search(query, **kwargs)
                    if not kwargs.get("text_only"):
                        visuals = index.unified_index.search(query, k=2, min_score=0.35,
                            sources=kwargs.get("sources"), modality="image")
                        hits += index.materialize_unified_results(visuals, k=2, min_score=0.35)
                    return hits

                index.search_results = search_with_pixels
            return index

    return FixtureService(store, backend="tfidf")
