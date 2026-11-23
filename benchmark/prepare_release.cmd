@echo off
setlocal
"%~dp0..\ComfyUI_windows_portable\python_embeded\python.exe" -B -X utf8 -u "%~dp0prepare_release.py" %*
exit /b %errorlevel%
