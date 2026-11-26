@echo off
chcp 65001 >nul
rem ============================================================
rem  Hugging Face 模型极速下载器 —— 启动
rem  双击即可，不会弹出黑色命令行窗口
rem  Python 用 Anaconda 环境里的那个（会自动在常见位置找）
rem  出问题看 “启动日志.log”，或双击 “诊断启动.bat”
rem ============================================================
setlocal
cd /d "%~dp0webui"

rem ---- 1. 手动指定优先（把 pythonw.exe 完整路径写进 python_path.txt 第一行）----
set "PYW="
if exist "%~dp0python_path.txt" set /p PYW=<"%~dp0python_path.txt"
if defined PYW if not exist "%PYW%" set "PYW="

rem ---- 2. 常见的 Anaconda / Miniconda 位置 ----
if not defined PYW if exist "%USERPROFILE%\anaconda3\pythonw.exe"  set "PYW=%USERPROFILE%\anaconda3\pythonw.exe"
if not defined PYW if exist "%LOCALAPPDATA%\anaconda3\pythonw.exe" set "PYW=%LOCALAPPDATA%\anaconda3\pythonw.exe"
if not defined PYW if exist "%ProgramData%\anaconda3\pythonw.exe"  set "PYW=%ProgramData%\anaconda3\pythonw.exe"
if not defined PYW if exist "C:\anaconda3\pythonw.exe"             set "PYW=C:\anaconda3\pythonw.exe"
if not defined PYW if exist "D:\anaconda3\pythonw.exe"             set "PYW=D:\anaconda3\pythonw.exe"
if not defined PYW if exist "E:\anaconda3\pythonw.exe"             set "PYW=E:\anaconda3\pythonw.exe"
if not defined PYW if exist "F:\anaconda3\pythonw.exe"             set "PYW=F:\anaconda3\pythonw.exe"
if not defined PYW if exist "%USERPROFILE%\miniconda3\pythonw.exe" set "PYW=%USERPROFILE%\miniconda3\pythonw.exe"

rem ---- 3. conda 登记过的所有环境 ----
if not defined PYW if exist "%USERPROFILE%\.conda\environments.txt" for /f "usebackq delims=" %%E in ("%USERPROFILE%\.conda\environments.txt") do if not defined PYW if exist "%%E\pythonw.exe" set "PYW=%%E\pythonw.exe"

rem ---- 4. 最后用 PATH 里的 pythonw ----
if not defined PYW for /f "delims=" %%P in ('where pythonw 2^>nul') do if not defined PYW set "PYW=%%P"

if not defined PYW (
  echo.
  echo 没有找到 pythonw.exe。
  echo 请把 Anaconda 里 pythonw.exe 的完整路径，写进本目录下的 python_path.txt 第一行，再双击本文件。
  echo.
  pause
  exit /b 1
)

>>"%~dp0启动日志.log" echo [启动.bat] %date% %time% 使用 %PYW%
start "" "%PYW%" "app.py" %*
exit /b 0
