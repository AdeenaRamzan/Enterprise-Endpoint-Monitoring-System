@echo off
python -m venv venv
call venv\Scripts\activate.bat
pip install -r requirements.txt
echo.
echo Setup complete. Check agent\config.json, then run start.bat.
pause
