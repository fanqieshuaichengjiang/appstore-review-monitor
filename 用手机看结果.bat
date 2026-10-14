@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem 想让「自己的手机」也能看结果时，双击这个。
rem 等价于： check_review_status.exe --lan --browser --port 8765
rem （--lan 开放给同 WiFi 访问；--browser 让本机也顺手打开浏览器）

if exist "check_review_status.exe" (
    check_review_status.exe --lan --browser --port 8765
) else (
    python check_review_status.py --lan --browser --port 8765
)

pause
