@echo off
setlocal
set "PYTHONUTF8=1"
"%~dp0..\ComfyUI_windows_portable\python_embeded\python.exe" -B -X utf8 -u "%~dp0minimax_ab.py" %*
exit /b %errorlevel%
