"""Retrieval and LLM orchestration for DocMind AI."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Dict, List, Sequence

from dotenv import load_dotenv
from openai import OpenAI
import ollama

from ingest import get_vectorstore


# Load env here too so this module can be imported independently in tests.
load_dotenv()
logger = logging.getLogger(__name__)

TOP_K = int(os.getenv("TOP_K", "5"))
MEMORY_WINDOW = int(os.getenv("MEMORY_WINDOW", "5"))
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").strip()
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3:latest").strip()


@dataclass
class RAGResult:
    """A small return object so the Flask layer can stream and persist safely."""

    text: str
    sources: List[str]
    provider: str


class RAGAgent:
    """Query documents, build the prompt, and fall back between model providers."""

    def __init__(self) -> None:
        # The vector store is loaded lazily so imports stay cheap.
        self._vectorstore = None
        self._openai_client = None
        self._ollama_client = None
        self.provider_name = "ollama"
        self.model_name = OLLAMA_MODEL

        if OPENAI_API_KEY:
            try:
                self._openai_client = OpenAI(base_url=OPENAI_BASE_URL, api_key=OPENAI_API_KEY)
                self.provider_name = "openai-compatible"
                self.model_name = OPENAI_MODEL
            except Exception as exc:  # pragma: no cover - defensive startup path
                logger.warning("OpenAI-compatible client could not be created: %s", exc)

        try:
            self._ollama_client = ollama.Client(host=OLLAMA_BASE_URL)
        except Exception as exc:  # pragma: no cover - defensive startup path
            logger.warning("Ollama client could not be created: %s", exc)

    def _vector_store(self):
        if self._vectorstore is None:
            self._vectorstore = get_vectorstore()
        return self._vectorstore

    def _is_greeting(self, question: str) -> bool:
        lowered = question.strip().lower()
        greeting_patterns = (
            r"^hi[!. ]*$",
            r"^hello[!. ]*$",
            r"^hey[!. ]*$",
            r"^how are you[?. ]*$",
            r"^good (morning|evening|afternoon)[!. ]*$",
        )
        return any(re.match(pattern, lowered) for pattern in greeting_patterns)

    def _format_sources(self, documents) -> List[str]:
        sources: List[str] = []
        seen = set()
        for doc in documents:
            meta = getattr(doc, "metadata", {}) or {}
            filename = meta.get("filename") or os.path.basename(str(meta.get("source", "document")))
            page = meta.get("page_number") or meta.get("page")
            label = f"{filename}"
            if page:
                label += f" (page {page})"
            if label not in seen:
                seen.add(label)
                sources.append(label)
        return sources

    def _retrieve_documents(self, question: str, selected_files: Sequence[str]):
        # Filter to the checked documents whenever the frontend sends them.
        search_kwargs = {"k": TOP_K}
        if selected_files:
            search_kwargs["filter"] = {"filename": {"$in": list(selected_files)}}

        try:
            return self._vector_store().similarity_search(question, **search_kwargs)
        except Exception as exc:
            logger.warning("Vector search failed, continuing without context: %s", exc)
            return []

    def _history_to_messages(self, history: Sequence[Dict[str, str]]):
        messages = []
        for item in history[-MEMORY_WINDOW * 2 :]:
            role = item.get("role", "")
            if role == "human":
                messages.append({"role": "user", "content": item.get("content", "")})
            elif role == "ai":
                messages.append({"role": "assistant", "content": item.get("content", "")})
        return messages

    def _build_messages(self, question: str, context: str, history: Sequence[Dict[str, str]]):
        messages = [
            {
                "role": "system",
                "content": (
                    "You are DocMind AI, a document question-answering assistant. "
                    "Use the provided document context first. If the answer is not in the context, "
                    "say so clearly instead of inventing details. Keep the tone concise and helpful."
                ),
            }
        ]
        messages.extend(self._history_to_messages(history))
        messages.append(
            {
                "role": "user",
                "content": (
                    "Document context:\n"
                    f"{context or 'No relevant document context was found.'}\n\n"
                    f"Question: {question}"
                ),
            }
        )
        return messages

    def _openai_complete(self, messages) -> str:
        if not self._openai_client:
            raise RuntimeError("OpenAI-compatible client is unavailable")
        response = self._openai_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=messages,
        )
        content = response.choices[0].message.content or ""
        self.provider_name = "openai-compatible"
        self.model_name = OPENAI_MODEL
        return content.strip()

    def _ollama_complete(self, messages) -> str:
        if not self._ollama_client:
            raise RuntimeError("Ollama client is unavailable")
        response = self._ollama_client.chat(
            model=OLLAMA_MODEL,
            messages=messages,
        )
        content = response.get("message", {}).get("content", "")
        self.provider_name = "ollama"
        self.model_name = OLLAMA_MODEL
        return content.strip()

    def answer(
        self,
        question: str,
        selected_files: Sequence[str],
        history: Sequence[Dict[str, str]],
    ) -> RAGResult:
        question = question.strip()
        if not question:
            return RAGResult(text="Please type a question.", sources=[], provider=self.provider_name)

        # Small talk should not trigger retrieval, which keeps the response quick.
        if self._is_greeting(question):
            messages = [
                {"role": "system", "content": "You are a friendly assistant."},
                {"role": "user", "content": question},
            ]
            try:
                return RAGResult(
                    text=self._openai_complete(messages),
                    sources=[],
                    provider=self.provider_name,
                )
            except Exception:
                return RAGResult(
                    text=self._ollama_complete(messages),
                    sources=[],
                    provider=self.provider_name,
                )

        documents = self._retrieve_documents(question, selected_files)
        sources = self._format_sources(documents)
        context = "\n\n".join(doc.page_content for doc in documents)
        messages = self._build_messages(question, context, history)

        # Try the OpenAI-compatible endpoint first, then drop back to Ollama.
        try:
            text = self._openai_complete(messages)
            provider = self.provider_name
        except Exception as openai_error:
            logger.warning("OpenAI-compatible request failed, falling back to Ollama: %s", openai_error)
            text = self._ollama_complete(messages)
            provider = self.provider_name

        if sources and "Sources:" not in text:
            text = f"{text}\n\nSources: {', '.join(sources)}"

        return RAGResult(text=text, sources=sources, provider=provider)
