# Starts the VoxPath backend on http://127.0.0.1:8000
# main.py loads backend/.env itself and selects the event loop psycopg needs on Windows.
Set-Location $PSScriptRoot
& .\.venv\Scripts\python.exe main.py
