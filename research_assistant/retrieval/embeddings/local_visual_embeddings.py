"""Local Chinese-CLIP adapter for text-to-image retrieval."""

from __future__ import annotations

import os
from pathlib import Path

import torch
import torch.nn.functional as functional
from PIL import Image
from transformers import ChineseCLIPModel, ChineseCLIPProcessor


DEFAULT_VISUAL_MODEL = "OFA-Sys/chinese-clip-vit-base-patch16"


class LocalChineseCLIPEmbeddings:
    """Encode Chinese/English queries and images into one shared vector space."""

    def __init__(self, model_name: str | None = None):
        self.model_name = model_name or os.getenv(
            "RESEARCH_ASSISTANT_VISUAL_MODEL", DEFAULT_VISUAL_MODEL
        )
        allow_download = os.getenv(
            "RESEARCH_ASSISTANT_ALLOW_MODEL_DOWNLOAD", "false"
        ).lower() in {"1", "true", "yes"}
        self.processor = ChineseCLIPProcessor.from_pretrained(
            self.model_name,
            local_files_only=not allow_download,
        )
        self.model = ChineseCLIPModel.from_pretrained(
            self.model_name,
            local_files_only=not allow_download,
        )
        self.model.eval()
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.model.to(self.device)
        self.dimension = int(self.model.config.projection_dim)

    @staticmethod
    def _tensor(value):
        if isinstance(value, torch.Tensor):
            return value
        for field in ("image_embeds", "text_embeds", "pooler_output"):
            candidate = getattr(value, field, None)
            if isinstance(candidate, torch.Tensor):
                return candidate
        raise TypeError("Chinese-CLIP did not return an embedding tensor")

    def embed_images(self, paths: list[Path]) -> list[list[float]]:
        vectors: list[list[float]] = []
        batch_size = int(os.getenv("RESEARCH_ASSISTANT_VISUAL_BATCH_SIZE", "8"))
        for start in range(0, len(paths), batch_size):
            images = []
            for path in paths[start : start + batch_size]:
                with Image.open(path) as image:
                    images.append(image.convert("RGB"))
            encoded = self.processor(images=images, return_tensors="pt")
            pixel_values = encoded["pixel_values"].to(self.device)
            with torch.no_grad():
                features = self._tensor(
                    self.model.get_image_features(pixel_values=pixel_values)
                )
            features = functional.normalize(features, p=2, dim=1)
            vectors.extend(features.cpu().tolist())
        return vectors

    def embed_query(self, text: str) -> list[float]:
        return self.embed_texts([text])[0]

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        batch_size = int(os.getenv("RESEARCH_ASSISTANT_VISUAL_TEXT_BATCH_SIZE", "16"))
        for start in range(0, len(texts), batch_size):
            batch = texts[start : start + batch_size]
            encoded = self.processor(
                text=batch,
                padding=True,
                truncation=True,
                max_length=512,
                return_tensors="pt",
            )
            inputs = {
                key: value.to(self.device)
                for key, value in encoded.items()
                if key in {"input_ids", "attention_mask", "token_type_ids"}
            }
            with torch.no_grad():
                features = self._tensor(self.model.get_text_features(**inputs))
            features = functional.normalize(features, p=2, dim=1)
            vectors.extend(features.cpu().tolist())
        return vectors
