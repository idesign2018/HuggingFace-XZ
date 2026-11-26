@echo off
chcp 65001 >nul
rem ============================================================
rem  诊断启动：用带控制台的 python 启动，任何报错都会显示在这里
rem  程序打不开时双击这个，把窗口里的内容截图即可
rem ============================================================
setlocal
cd /d "%~dp0webui"

set "PY="
if exist "%~dp0python_path.txt" set /p PY=<"%~dp0python_path.txt"
if defined PY set "PY=%PY:pythonw.exe=python.exe%"
if defined PY if not exist "%PY%" set "PY="
if not defined PY if exist "%USERPROFILE%\anaconda3\python.exe"  set "PY=%USERPROFILE%\anaconda3\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\anaconda3\python.exe" set "PY=%LOCALAPPDATA%\anaconda3\python.exe"
if not defined PY if exist "C:\anaconda3\python.exe"             set "PY=C:\anaconda3\python.exe"
if not defined PY if exist "D:\anaconda3\python.exe"             set "PY=D:\anaconda3\python.exe"
if not defined PY for /f "delims=" %%P in ('where python 2^>nul') do if not defined PY set "PY=%%P"

if not defined PY (
  echo 没有找到 python.exe。
  pause
  exit /b 1
)

echo 使用的 Python：%PY%
echo.
echo ============ 1/2 自检 ============
"%PY%" app.py --check
echo.
echo ============ 2/2 启动 ============
echo （这个黑色窗口不要关，关掉它程序就退出了）
echo.
"%PY%" app.py %*
echo.
echo ============ 程序已退出 ============
echo 如果上面有报错，请截图发给开发者；也可以查看 启动日志.log
pause
