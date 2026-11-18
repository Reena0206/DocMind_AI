"""
ingest.py  â€”  Document Ingestion Pipeline (Windows / Python 3.11)
FIX v4:
  - langchain_postgres PGVector (schema: id column, not uuid)
  - create_tables_if_not_exists=True â†’ auto table creation on first run
  - Schema mismatch fix: avoids conflicts with older langchain_community tables
  - Pre-migration helper: migrate_schema() function added
"""

import os
import logging
from pathlib import Path
from typing import List

import pytesseract
from pdf2image import convert_from_path
from dotenv import load_dotenv

from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain_community.document_loaders import (
    UnstructuredPDFLoader,
    Docx2txtLoader,
    TextLoader,
)
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_postgres.vectorstores import PGVector
from langchain.schema import Document

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

TESSERACT_CMD = os.getenv("TESSERACT_CMD", r"C:\Program Files\Tesseract-OCR\tesseract.exe")
POPPLER_PATH  = os.getenv("POPPLER_PATH",  r"C:\poppler\Library\bin")
pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD
logger.info("Tesseract: %s", TESSERACT_CMD)
logger.info("Poppler  : %s", POPPLER_PATH)

CHUNK_SIZE    = 512
CHUNK_OVERLAP = 64
EMBED_MODEL   = "sentence-transformers/all-MiniLM-L6-v2"
COLLECTION    = "rag_documents"

# FIX: psycopg3 driver (psycopg, not psycopg2) for langchain_postgres
PG_CONN = (
    f"postgresql+psycopg://{os.getenv('PG_USER','postgres')}:"
    f"{os.getenv('PG_PASSWORD','password')}@"
    f"{os.getenv('PG_HOST','localhost')}:"
    f"{os.getenv('PG_PORT','5432')}/"
    f"{os.getenv('PG_DB','ragdb')}"
)

logger.info("Loading BERT model... (first run may take ~1-2 minutes)")
embeddings = HuggingFaceEmbeddings(
    model_name=EMBED_MODEL,
    model_kwargs={"device": "cpu"},
    encode_kwargs={"normalize_embeddings": True},
)
logger.info("Embeddings ready!")


# â”€â”€ Schema Migration Helper â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# RUN THIS ONCE if you get:
#   "column 'id' of relation 'langchain_pg_embedding' does not exist"
# This drops old tables (langchain_community schema) and lets langchain_postgres
# recreate them with the correct schema on next ingest.
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def migrate_schema():
    """
    Drop the older langchain_community tables.
    Call this once if you see a column "id" does not exist error.

    Usage:
        python -c "from ingest import migrate_schema; migrate_schema()"
    """
    import psycopg
    conn_str = (
        f"host={os.getenv('PG_HOST','localhost')} "
        f"port={os.getenv('PG_PORT','5432')} "
        f"dbname={os.getenv('PG_DB','ragdb')} "
        f"user={os.getenv('PG_USER','postgres')} "
        f"password={os.getenv('PG_PASSWORD','password')}"
    )
    try:
        with psycopg.connect(conn_str) as conn:
            with conn.cursor() as cur:
                logger.warning("Schema migration: dropping legacy tables...")
                cur.execute("DROP TABLE IF EXISTS langchain_pg_embedding CASCADE;")
                cur.execute("DROP TABLE IF EXISTS langchain_pg_collection CASCADE;")
                conn.commit()
        logger.info("Migration complete. langchain_postgres will recreate the schema.")
        logger.info("Run ingestion again after migration.")
    except Exception as e:
        logger.error("Migration failed: %s", e)
        raise


# â”€â”€ OCR â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def extract_text_ocr(pdf_path: str) -> str:
    logger.info("Tesseract OCR: %s", pdf_path)
    try:
        images = convert_from_path(pdf_path, dpi=200, poppler_path=POPPLER_PATH)
    except Exception as e:
        raise RuntimeError(
            f"pdf2image failed.\nPoppler path: {POPPLER_PATH}\n"
            f"Download: https://github.com/oschwartz10612/poppler-windows/releases\n"
            f"Error: {e}"
        )
    pages = []
    for i, img in enumerate(images, 1):
        text = pytesseract.image_to_string(img, lang="eng")
        pages.append(f"[Page {i}]\n{text.strip()}")
        logger.info("  Page %d/%d OCR done", i, len(images))
    return "\n\n".join(pages)


# â”€â”€ Loader â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def load_document(file_path: str, display_name: str | None = None) -> List[Document]:
    path = Path(file_path)
    ext  = path.suffix.lower()
    docs: List[Document] = []
    logger.info("Loading: %s", path.name)

    if ext == ".pdf":
        try:
            loader = UnstructuredPDFLoader(str(file_path), mode="elements")
            docs   = loader.load()
        except Exception as e:
            logger.warning("PDF loader failed: %s", e)
            docs = []
        combined = " ".join(d.page_content for d in docs).strip()
        if len(combined) < 100:
            # Text-based PDFs can be read with pdfminer alone, without Poppler or OCR.
            try:
                from pdfminer.high_level import extract_text
                text = extract_text(str(file_path)).strip()
            except Exception as e:
                logger.warning("pdfminer failed: %s", e)
                text = ""
            if len(text) >= 100:
                docs = [Document(
                    page_content=text,
                    metadata={"source": str(file_path), "loader": "pdfminer"},
                )]
                combined = text
        if len(combined) < 100:
            logger.warning("Image-based PDF detected (%d chars) — using OCR", len(combined))
            ocr_text = extract_text_ocr(str(file_path))
            docs = [Document(
                page_content=ocr_text,
                metadata={"source": str(file_path), "loader": "tesseract_ocr"},
            )]
        else:
            logger.info("Standard PDF detected — %d characters extracted", len(combined))
    elif ext in (".docx", ".doc"):
        loader = Docx2txtLoader(str(file_path))
        docs   = loader.load()
    elif ext == ".txt":
        for enc in ["utf-8", "utf-8-sig", "cp1252", "latin-1"]:
            try:
                loader = TextLoader(str(file_path), encoding=enc)
                docs   = loader.load()
                logger.info("TXT loaded with encoding: %s", enc)
                break
            except UnicodeDecodeError:
                continue
    elif ext in (".png", ".jpg", ".jpeg"):
        logger.info("Image detected — using OCR")
        try:
            from PIL import Image
            img = Image.open(str(file_path))
            text = pytesseract.image_to_string(img, lang="eng")
            docs = [Document(
                page_content=text,
                metadata={"source": str(file_path), "loader": "tesseract_ocr", "page_number": 1},
            )]
        except Exception as e:
            logger.error("Image OCR failed: %s", e)
            raise RuntimeError(f"Image OCR failed: {e}")
    else:
        raise ValueError(f"Unsupported: {ext}. Use .pdf, .docx, .doc, .txt, .png, .jpg, .jpeg")

    # Store the user-facing name so retrieval filters match the files selected in the UI.
    for doc in docs:
        doc.metadata["filename"] = display_name or path.name
    logger.info("Loaded %d sections from %s", len(docs), path.name)
    return docs


# â”€â”€ Splitter â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def split_documents(docs: List[Document]) -> List[Document]:
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
        separators=["\n\n", "\n", ". ", "! ", "? ", " ", ""],
    )
    chunks = splitter.split_documents(docs)
    logger.info("%d chunks created", len(chunks))
    return chunks


# â”€â”€ pgvector save â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def store_to_pgvector(chunks: List[Document]) -> PGVector:
    logger.info("Saving %d chunks...", len(chunks))
    try:
        vs = PGVector.from_documents(
            documents=chunks,
            embedding=embeddings,
            collection_name=COLLECTION,
            connection=PG_CONN,
            pre_delete_collection=False,
            use_jsonb=True,
        )
        logger.info("pgvector save complete!")
        return vs
    except Exception as e:
        err = str(e)
        # Detect the old schema mismatch and return clear recovery steps.
        if 'column "id" of relation "langchain_pg_embedding" does not exist' in err:
            raise RuntimeError(
                "\n"
                "â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”\n"
                "âŒ  SCHEMA MISMATCH — legacy table found in the database!\n"
                "â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”\n"
                "To fix it, run this once:\n\n"
                "  python -c \"from ingest import migrate_schema; migrate_schema()\"\n\n"
                "Or run these commands in psql:\n"
                "  DROP TABLE IF EXISTS langchain_pg_embedding CASCADE;\n"
                "  DROP TABLE IF EXISTS langchain_pg_collection CASCADE;\n\n"
                "Then run ingestion again.\n"
                "â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”\n"
            )
        raise RuntimeError(
            f"pgvector error!\n"
            f"Is PostgreSQL + pgvector running?\n"
            f"  psql: CREATE EXTENSION IF NOT EXISTS vector;\n"
            f"Error: {e}"
        )


# â”€â”€ Main entry â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def ingest_file(file_path: str, display_name: str | None = None) -> dict:
    docs   = load_document(file_path, display_name)
    chunks = split_documents(docs)
    store_to_pgvector(chunks)
    return {
        "filename":        Path(file_path).name,
        "sections_loaded": len(docs),
        "chunks_stored":   len(chunks),
    }


def get_vectorstore() -> PGVector:
    return PGVector(
        collection_name=COLLECTION,
        connection=PG_CONN,
        embeddings=embeddings,
        use_jsonb=True,
    )



