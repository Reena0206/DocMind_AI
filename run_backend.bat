@echo off
echo Starting Unified RAG AI Agent (Flask + Custom HTML UI)...
call rag_env\Scripts\activate
cd backend
python main.py
pause
