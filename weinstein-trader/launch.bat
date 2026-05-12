@echo off
title Weinstein Trader
echo.
echo  Starting Weinstein Trader...
echo.

pip install flask flask-cors anthropic ib_insync --quiet --disable-pip-version-check

echo  Opening app in your browser...
python app.py

pause
