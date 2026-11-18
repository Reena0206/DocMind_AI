"""Flask entrypoint for the DocMind AI application."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

import psycopg2
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template, request, session, stream_with_context
from flask_cors import CORS
from psycopg2.extras import RealDictCursor
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from ingest import ingest_file
from retriever import RAGAgent


# Load environment variables before anything else so local and deployed runs match.
load_dotenv()

app = Flask(__name__)
CORS(app, supports_credentials=True)

# Keep the session secret configurable, but provide a sane default for dev.
app.secret_key = os.getenv("FLASK_SECRET_KEY", "docmind-dev-secret")
app.config["UPLOAD_FOLDER"] = os.getenv("UPLOAD_FOLDER", str(Path(__file__).parent / "uploads"))

UPLOAD_DIR = Path(app.config["UPLOAD_FOLDER"])
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_EXTENSIONS = {".pdf", ".docx", ".doc", ".txt", ".png", ".jpg", ".jpeg"}
MEMORY_WINDOW = int(os.getenv("MEMORY_WINDOW", "5"))


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _db_params() -> Dict[str, Any]:
    # Read database settings lazily so env edits do not require a code change.
    return {
        "host": os.getenv("PG_HOST", "localhost"),
        "port": _int_env("PG_PORT", 5432),
        "dbname": os.getenv("PG_DB", "ragdb"),
        "user": os.getenv("PG_USER", "postgres"),
        "password": os.getenv("PG_PASSWORD", "password"),
    }


@contextmanager
def db_cursor():
    # A tiny transaction helper keeps the route handlers uncluttered.
    conn = psycopg2.connect(**_db_params())
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


def get_db_conn():
    # Retained for older code paths that expect a raw connection object.
    return psycopg2.connect(**_db_params())


def _serialize(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _serialize_row(row: Dict[str, Any]) -> Dict[str, Any]:
    return {key: _serialize(value) for key, value in row.items()}


def _allowed_file(filename: str) -> bool:
    return Path(filename).suffix.lower() in ALLOWED_EXTENSIONS


def _ensure_user_logged_in() -> Optional[Dict[str, Any]]:
    user_id = session.get("user_id")
    if not user_id:
        return None
    with db_cursor() as cur:
        cur.execute(
            "SELECT id, username, created_at FROM users WHERE id = %s",
            (user_id,),
        )
        user = cur.fetchone()
    return _serialize_row(user) if user else None


def init_db() -> None:
    # Create the relational tables up front so the app can run on a fresh DB.
    with db_cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username VARCHAR(100) UNIQUE NOT NULL,
                password_hash VARCHAR(256) NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                id SERIAL PRIMARY KEY,
                user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                title VARCHAR(255) NOT NULL DEFAULT 'New Chat',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id SERIAL PRIMARY KEY,
                conversation_id INT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                role VARCHAR(50) NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS user_files (
                id SERIAL PRIMARY KEY,
                user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                filename VARCHAR(255) NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, filename)
            );
            """
        )


def _require_user() -> Dict[str, Any]:
    user = _ensure_user_logged_in()
    if not user:
        raise PermissionError("Authentication required")
    return user


def _get_or_create_conversation(user_id: int, conversation_id: Optional[int] = None) -> int:
    with db_cursor() as cur:
        if conversation_id:
            cur.execute(
                "SELECT id FROM conversations WHERE id = %s AND user_id = %s",
                (conversation_id, user_id),
            )
            row = cur.fetchone()
            if row:
                return int(row["id"])

        cur.execute(
            "INSERT INTO conversations (user_id, title) VALUES (%s, %s) RETURNING id",
            (user_id, "New Chat"),
        )
        return int(cur.fetchone()["id"])


def _conversation_messages(conversation_id: int, limit: int = MEMORY_WINDOW) -> List[Dict[str, Any]]:
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT role, content, created_at
            FROM messages
            WHERE conversation_id = %s
            ORDER BY id DESC
            LIMIT %s
            """,
            (conversation_id, limit),
        )
        rows = cur.fetchall()
    rows.reverse()
    return [_serialize_row(row) for row in rows]


def _store_message(conversation_id: int, role: str, content: str) -> None:
    with db_cursor() as cur:
        cur.execute(
            "INSERT INTO messages (conversation_id, role, content) VALUES (%s, %s, %s)",
            (conversation_id, role, content),
        )


def _maybe_update_title(conversation_id: int, question: str) -> None:
    title = question.strip().replace("\n", " ")[:35] or "New Chat"
    with db_cursor() as cur:
        cur.execute(
            """
            UPDATE conversations
            SET title = %s
            WHERE id = %s AND title = 'New Chat'
            """,
            (title, conversation_id),
        )


def _list_user_files(user_id: int) -> List[Dict[str, Any]]:
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT filename, created_at
            FROM user_files
            WHERE user_id = %s
            ORDER BY created_at DESC, filename ASC
            """,
            (user_id,),
        )
        rows = cur.fetchall()
    return [_serialize_row(row) for row in rows]


def _sse_payload(payload: Dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def get_agent() -> RAGAgent:
    # The agent is created lazily so startup stays snappy.
    if not hasattr(app, "_rag_agent"):
        app._rag_agent = RAGAgent()
    return app._rag_agent


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/health")
def health():
    agent = get_agent()
    return jsonify(
        {
            "status": "ok",
            "llm_provider": agent.provider_name,
            "llm_model": agent.model_name,
        }
    )


@app.route("/user")
def current_user():
    user = _ensure_user_logged_in()
    if not user:
        return jsonify({"authenticated": False}), 401
    return jsonify({"authenticated": True, "user": user})


@app.route("/auth", methods=["POST"])
def auth():
    data = request.get_json(force=True, silent=True) or request.form.to_dict()
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    mode = (data.get("mode") or "login").strip().lower()

    if not username or not password:
        return jsonify({"ok": False, "error": "Username and password are required."}), 400

    with db_cursor() as cur:
        cur.execute("SELECT id, username, password_hash, created_at FROM users WHERE username = %s", (username,))
        existing = cur.fetchone()

        if mode == "register":
            if existing:
                return jsonify({"ok": False, "error": "Username already exists."}), 409
            cur.execute(
                "INSERT INTO users (username, password_hash) VALUES (%s, %s) RETURNING id, username, created_at",
                (username, generate_password_hash(password)),
            )
            user = cur.fetchone()
        else:
            if not existing or not check_password_hash(existing["password_hash"], password):
                return jsonify({"ok": False, "error": "Invalid username or password."}), 401
            user = existing

    session["user_id"] = int(user["id"])
    session["username"] = user["username"]
    session["conversation_id"] = None
    return jsonify({"ok": True, "user": _serialize_row(user)})


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/conversations", methods=["GET", "POST"])
def conversations():
    user = _ensure_user_logged_in()
    if not user:
        return jsonify({"ok": False, "error": "Login required."}), 401

    if request.method == "POST":
        conversation_id = _get_or_create_conversation(int(user["id"]))
        session["conversation_id"] = conversation_id
        with db_cursor() as cur:
            cur.execute(
                "SELECT id, title, created_at FROM conversations WHERE id = %s",
                (conversation_id,),
            )
            row = cur.fetchone()
        return jsonify({"ok": True, "conversation": _serialize_row(row)})

    with db_cursor() as cur:
        cur.execute(
            """
            SELECT id, title, created_at
            FROM conversations
            WHERE user_id = %s
            ORDER BY created_at DESC, id DESC
            """,
            (int(user["id"]),),
        )
        rows = cur.fetchall()
    return jsonify({"ok": True, "conversations": [_serialize_row(row) for row in rows]})



@app.route("/conversations/<int:conversation_id>", methods=["DELETE"])
def delete_conversation(conversation_id: int):
    user = _ensure_user_logged_in()
    if not user:
        return jsonify({"ok": False, "error": "Login required."}), 401

    with db_cursor() as cur:
        cur.execute(
            "DELETE FROM conversations WHERE id = %s AND user_id = %s",
            (conversation_id, int(user["id"])),
        )
    if session.get("conversation_id") == conversation_id:
        session.pop("conversation_id", None)
    return jsonify({"ok": True})

@app.route("/conversations/<int:conversation_id>/messages", methods=["GET"])
def conversation_messages(conversation_id: int):
    user = _ensure_user_logged_in()
    if not user:
        return jsonify({"ok": False, "error": "Login required."}), 401

    with db_cursor() as cur:
        cur.execute(
            """
            SELECT m.id, m.role, m.content, m.created_at
            FROM messages m
            JOIN conversations c ON c.id = m.conversation_id
            WHERE m.conversation_id = %s AND c.user_id = %s
            ORDER BY m.id ASC
            """,
            (conversation_id, int(user["id"])),
        )
        rows = cur.fetchall()
    return jsonify({"ok": True, "messages": [_serialize_row(row) for row in rows]})


@app.route("/files", methods=["GET"])
def files():
    user = _ensure_user_logged_in()
    if not user:
        return jsonify({"ok": False, "error": "Login required."}), 401
    return jsonify({"ok": True, "files": _list_user_files(int(user["id"]))})


@app.route("/upload", methods=["POST"])
def upload():
    user = _ensure_user_logged_in()
    if not user:
        return jsonify({"ok": False, "error": "Login required."}), 401

    uploaded = request.files.getlist("files") or list(request.files.values())
    if not uploaded:
        return jsonify({"ok": False, "error": "No files were sent."}), 400

    results: List[Dict[str, Any]] = []
    for storage in uploaded:
        if not storage.filename:
            continue
        if not _allowed_file(storage.filename):
            results.append({"filename": storage.filename, "ok": False, "error": "Unsupported file type."})
            continue

        safe_name = secure_filename(storage.filename)
        save_name = f"{uuid4().hex}_{safe_name}"
        save_path = UPLOAD_DIR / save_name
        storage.save(str(save_path))

        try:
            ingest_summary = ingest_file(str(save_path), safe_name)
            with db_cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO user_files (user_id, filename)
                    VALUES (%s, %s)
                    ON CONFLICT (user_id, filename) DO UPDATE
                    SET created_at = CURRENT_TIMESTAMP
                    """,
                    (int(user["id"]), safe_name),
                )
            results.append({"filename": safe_name, "ok": True, "ingest": ingest_summary})
        except Exception as exc:
            results.append({"filename": safe_name, "ok": False, "error": str(exc)})

    return jsonify({"ok": True, "results": results, "files": _list_user_files(int(user["id"]))})


@app.route("/memory/clear", methods=["POST"])
def clear_memory():
    user = _ensure_user_logged_in()
    if not user:
        return jsonify({"ok": False, "error": "Login required."}), 401
    session.pop("conversation_id", None)
    return jsonify({"ok": True})


@app.route("/chat/stream", methods=["POST"])
def chat_stream():
    user = _ensure_user_logged_in()
    if not user:
        return jsonify({"ok": False, "error": "Login required."}), 401

    data = request.get_json(force=True, silent=True) or {}
    question = (data.get("message") or "").strip()
    selected_files = [name for name in (data.get("files") or []) if name]
    conversation_id = data.get("conversation_id") or session.get("conversation_id")

    if not question:
        return jsonify({"ok": False, "error": "Message is required."}), 400

    if not selected_files and not _is_greeting(question):
        return jsonify({"ok": False, "error": "Select at least one document first."}), 400

    conversation_id = _get_or_create_conversation(int(user["id"]), int(conversation_id) if conversation_id else None)
    session["conversation_id"] = conversation_id
    _maybe_update_title(conversation_id, question)
    _store_message(conversation_id, "human", question)

    history = _conversation_messages(conversation_id, limit=MEMORY_WINDOW)
    agent = get_agent()

    def generate():
        # Build the full answer first, then stream it in lightweight chunks.
        completed = False
        try:
            result = agent.answer(question=question, selected_files=selected_files, history=history)
            final_text = result.text.strip() or "I could not generate a response."

            words = final_text.split()
            if not words:
                yield _sse_payload({"type": "token", "delta": final_text})
            else:
                for word in words:
                    yield _sse_payload({"type": "token", "delta": word + " "})

            _store_message(conversation_id, "ai", final_text)
            completed = True
            yield _sse_payload(
                {
                    "type": "done",
                    "text": final_text,
                    "provider": result.provider,
                    "sources": result.sources,
                }
            )
        except GeneratorExit:
            if not completed:
                _store_message(conversation_id, "ai", "Canceled")
            raise
        except Exception as exc:
            error_text = f"Error: {exc}"
            _store_message(conversation_id, "ai", error_text)
            completed = True
            yield _sse_payload({"type": "error", "error": error_text})

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def _is_greeting(text: str) -> bool:
    lowered = text.strip().lower()
    return lowered in {"hi", "hello", "hey", "how are you", "good morning", "good evening"}


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")), debug=True)



