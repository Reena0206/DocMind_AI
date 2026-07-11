@echo off
echo ===================================================
echo RAG AI Agent - Setup Script
echo ===================================================

:: Check if Python is installed
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python is not installed or not in PATH!
    echo Please install Python (recommended 3.10 or 3.11) and try again.
    pause
    exit /b 1
)

:: Recreate virtual environment if broken
echo Checking virtual environment...
if exist "rag_env" (
    echo Deleting existing/broken virtual environment...
    rmdir /s /q "rag_env"
)

echo Creating a new virtual environment in 'rag_env'...
python -m venv rag_env
if %errorlevel% neq 0 (
    echo [ERROR] Failed to create virtual environment.
    pause
    exit /b 1
)

echo Activating virtual environment...
call rag_env\Scripts\activate

echo Upgrading pip...
python -m pip install --upgrade pip

echo Installing PyTorch (CPU version - faster download and execution)...
pip install torch==2.3.1 --index-url https://download.pytorch.org/whl/cpu

echo Installing remaining requirements...
pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo [ERROR] Failed to install requirements.
    pause
    exit /b 1
)

echo ===================================================
echo Setup completed successfully!
echo ===================================================
pause


