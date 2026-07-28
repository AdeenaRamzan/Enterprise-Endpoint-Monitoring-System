@echo off
REM Run once. Creates a virtual environment and installs everything
REM the backend needs from requirements.txt -- one command, nothing
REM to type or remember individually.
python -m venv venv
call venv\Scripts\activate.bat
pip install -r requirements.txt
echo.
echo Setup complete. Next: create backend\.env (see SETUP_GUIDE.md step 4),
echo then run start.bat to launch the backend.
pause
