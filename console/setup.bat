@echo off
python -m venv venv
call venv\Scripts\activate.bat
pip install -r requirements.txt
echo.
echo Setup complete. Double-click start.bat any time to open the console.
pause
