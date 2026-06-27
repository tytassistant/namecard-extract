@echo off
title 1 - Namecard Extract Server

echo [1/2] Cleaning up any old/stuck server processes on port 8001...
wsl ~ -d Ubuntu -u tytassistant bash -c "pkill -f 'uvicorn main:app.*8001' 2>/dev/null; fuser -k 8001/tcp 2>/dev/null; true"
timeout /t 2 /nobreak >nul

echo [2/2] Launching Namecard Extract FastAPI...
wsl ~ -d Ubuntu -u tytassistant bash -c "cd /home/tytassistant/programs/namecard-extract/FastAPI; PYTHONUNBUFFERED=1 ./venv/bin/python -m uvicorn main:app --host 0.0.0.0 --port 8001 --reload 2>&1 | tee server.log"

echo.
echo [INFO] Server has been shut down.
pause
