from functools import cached_property
import time
from typing import Any

from pinecone import Pinecone, ServerlessSpec

from app.config import settings


class VectorStore:
    def __init__(self) -> None:
        self.client = Pinecone(api_key=settings.pinecone_api_key)

    def ensure_index(self) -> None:
        if settings.pinecone_index not in self.client.list_indexes().names():
            self.client.create_index(
                name=settings.pinecone_index,
                dimension=settings.embedding_dimension,
                metric="cosine",
                spec=ServerlessSpec(cloud=settings.pinecone_cloud, region=settings.pinecone_region),
            )
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                if self.client.describe_index(settings.pinecone_index).status.get("ready"):
                    break
                time.sleep(2)
            else:
                raise TimeoutError("Pinecone index was not ready after 120 seconds")

    @cached_property
    def index(self):
        self.ensure_index()
        return self.client.Index(settings.pinecone_index)

    def upsert(self, namespace: str, vectors: list[dict[str, Any]]) -> None:
        for offset in range(0, len(vectors), 100):
            self.index.upsert(vectors=vectors[offset:offset + 100], namespace=namespace)

    def query(self, namespace: str, vector: list[float], top_k: int, filter_: dict | None) -> list[Any]:
        result = self.index.query(
            namespace=namespace, vector=vector, top_k=top_k,
            include_metadata=True, filter=filter_,
        )
        return list(result.matches or [])

    def delete_document(self, namespace: str, document_id: str) -> None:
        try:
            self.index.delete(namespace=namespace, filter={"document_id": {"$eq": document_id}})
        except Exception as exc:
            message = str(exc).lower()
            if "namespace not found" not in message and "status: 404" not in message:
                raise


vector_store = VectorStore()
