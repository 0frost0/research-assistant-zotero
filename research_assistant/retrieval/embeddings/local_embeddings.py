"""直接用 Transformers 加载本地 BGE，避开当前损坏的高层封装。"""

import os

import torch
import torch.nn.functional as functional
from langchain_core.embeddings import Embeddings
from transformers import AutoModel, AutoTokenizer


class LocalBGEEmbeddings(Embeddings):
    """把本地 BGE 模型适配为 LangChain Embeddings 接口。"""

    def __init__(self, model_name: str | None = None):
        model_name = model_name or os.getenv(
            "RESEARCH_ASSISTANT_EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5"
        )
        self.model_name = model_name
        self.tokenizer = AutoTokenizer.from_pretrained(
            model_name,
            local_files_only=True,
        )
        self.model = AutoModel.from_pretrained(
            model_name,
            local_files_only=True,
        )
        self.model.eval()
        self.dimension = int(self.model.config.hidden_size)

    def _embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        batch_size = 16
        for start in range(0, len(texts), batch_size):
            batch = texts[start : start + batch_size]
            encoded = self.tokenizer(
                batch,
                padding=True,
                truncation=True,
                max_length=512,
                return_tensors="pt",
            )
            with torch.no_grad():
                output = self.model(**encoded)

            # BGE 使用首个 token 的向量表示整段文本，再做归一化。
            embeddings = output.last_hidden_state[:, 0]
            embeddings = functional.normalize(embeddings, p=2, dim=1)
            vectors.extend(embeddings.cpu().tolist())
        return vectors

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts)

    def embed_query(self, text: str) -> list[float]:
        return self._embed([text])[0]
