# RAG AI Agent — Windows Setup Guide (Python 3.11)

## One-time setup

---

### STEP 1 — Install PostgreSQL + pgvector

1. Download PostgreSQL:
   https://www.enterprisedb.com/downloads/postgres-postgresql-downloads
   (choose version 15 or 16)

2. Install it with the default settings.
   Keep the password somewhere safe because you will add it to `.env`.

3. Install the pgvector extension:
   - Open pgAdmin 4 from the Start Menu
   - Go to Servers → PostgreSQL → Databases → right click → Create → Database
   - Name it `ragdb` and save
   - Click `ragdb` → Query Tool
   - Run this SQL:
     ```sql
     CREATE EXTENSION IF NOT EXISTS vector;
     ```
   - Press F5 and confirm that "Commands completed successfully" appears

---

### STEP 2 — Install Poppler for PDF image support

1. Download it from: https://github.com/oschwartz10612/poppler-windows/releases
   (latest release → `poppler-xx.xx.x_x86.7z`)

2. Extract it to `C:\poppler\`

3. Update the path in your `.env` file:
   ```
   POPPLER_PATH=C:\poppler\Library\bin
   ```

---

### STEP 3 — Verify Tesseract is installed

Make sure Tesseract is on your PATH:
```
Win + R → cmd → tesseract --version
```

If it is not detected:
- Open System Properties → Environment Variables → Path → Edit
- Add: `C:\Program Files\Tesseract-OCR`

---

### STEP 4 — Start Ollama

If llama3 is already downloaded, start Ollama:
```
ollama serve
```

(Or let Windows run it in the background if you already configured that.)

---

### STEP 5 — Project setup

1. Keep this project folder somewhere convenient, for example: `C:\rag_agent\`

2. Open `.env` and add your password:
   ```
   PG_PASSWORD=your_postgresql_password
   TESSERACT_CMD=C:\Program Files\Tesseract-OCR\tesseract.exe
   POPPLER_PATH=C:\poppler\Library\bin
   ```

3. Run `setup_windows.bat` by double-clicking it.
   It will create the virtual environment and install everything automatically.

---

### STEP 6 — Run the project

Terminal 1 (Backend):
```
run_backend.bat
```

Browser:
```
http://localhost:8000
```

---

## Project Structure

```
C:\rag_agent\
│
├── setup_windows.bat      ← Run once
├── run_backend.bat        ← Start the backend
├── requirements.txt
│
├── backend\
│   ├── main.py            ← Flask server
│   ├── ingest.py          ← Document loading + OCR + BERT + pgvector
│   ├── retriever.py       ← Retrieval + memory + Ollama/OpenAI-compatible LLM
│   └── .env               ← Config (password goes here)
│
└── frontend\
    └── app.py             ← If your build includes a separate frontend entrypoint
```

---

## How it works

```
1. Document upload (PDF/DOCX/TXT)
         ↓
2. File type detection
   PDF text?  → UnstructuredPDFLoader
   PDF image? → Tesseract OCR (pytesseract + pdf2image + Poppler)
   DOCX?      → Docx2txtLoader
   TXT?       → TextLoader (UTF-8 / cp1252)
         ↓
3. RecursiveCharacterTextSplitter
   chunk_size=512, overlap=64
         ↓
4. BERT embeddings
   sentence-transformers/all-MiniLM-L6-v2
         ↓
5. PostgreSQL pgvector storage
   (normalized vectors for cosine similarity)

--- Query time ---

6. User question → BERT embed
         ↓
7. pgvector cosine similarity → top-5 chunks
         ↓
8. Last 5 conversation turns from memory
         ↓
9. Ollama llama3 or OpenAI-compatible model → streamed response
         ↓
10. Source chunks displayed in the UI
```

---

## API Endpoints

| Method | URL                 | Purpose                        |
|--------|---------------------|--------------------------------|
| GET    | /health             | Status check                   |
| POST   | /upload             | File upload + ingestion        |
| POST   | /query              | Full answer + source chunks    |
| GET    | /stream?question=.. | SSE streaming response         |
| DELETE | /memory             | Clear conversation memory      |
| GET    | /history            | Show the last 5 turns          |
| GET    | /stats              | Database chunk count           |

Swagger UI: http://localhost:8000/docs

---

## Common Windows Errors and Fixes

| Error | Fix |
|-------|-----|
| `tesseract is not recognized` | Add `C:\Program Files\Tesseract-OCR` to PATH |
| `Unable to get page count` | Install Poppler or set `POPPLER_PATH` |
| `connection refused port 5432` | Start the PostgreSQL service |
| `connection refused port 11434` | Run `ollama serve` in the terminal |
| `torch not found` | `pip install torch --index-url https://download.pytorch.org/whl/cpu` |
| `ModuleNotFoundError: unstructured` | `pip install unstructured[pdf]` |
| Slow first query | This is normal; the model may take longer on the first load |

---

## Memory + Performance Tips

- With 64GB RAM, the full stack should feel fast
- BERT model uses about 90MB RAM
- llama3 uses around 8GB RAM
- pgvector index may be cached in memory
- For faster responses, change `OLLAMA_MODEL=llama3` to a smaller model such as `mistral`

