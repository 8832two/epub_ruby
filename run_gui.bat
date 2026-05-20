@echo off
cd /d "%~dp0"

:: --------------------------------------------------
::  epub-ruby GUI 一键启动 (Windows)
:: --------------------------------------------------
title epub-ruby GUI

set "VENV_DIR=.venv"

:: -- 检查 Python ------------------------------------
where python >nul 2>&1
if errorlevel 1 goto NO_PYTHON

echo Python found:
python --version
echo.

:: -- 创建 venv --------------------------------------
if exist "%VENV_DIR%\Scripts\python.exe" goto VENV_OK

echo Creating virtual environment ...
python -m venv "%VENV_DIR%"
if errorlevel 1 goto VENV_FAIL
if exist "%VENV_DIR%\.deps_installed" del "%VENV_DIR%\.deps_installed"
echo Done.
goto ACTIVATE

:VENV_OK
echo Virtual environment ready.

:: -- 激活 -------------------------------------------
:ACTIVATE
call "%VENV_DIR%\Scripts\activate.bat"
if errorlevel 1 goto ACTIVATE_FAIL

:: -- 安装/更新依赖（每次运行都刷新 editable install） --
echo Checking dependencies ...
echo.

:: -- 检测国内镜像 (清华 TUNA) -----------------------
set "MIRROR="
curl -s --connect-timeout 3 https://pypi.tuna.tsinghua.edu.cn >nul 2>&1
if not errorlevel 1 (
    set "MIRROR=-i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn"
    echo Using Tsinghua mirror for faster download in China.
) else (
    curl -s --connect-timeout 3 https://mirrors.aliyun.com >nul 2>&1
    if not errorlevel 1 (
        set "MIRROR=-i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com"
        echo Using Aliyun mirror for faster download in China.
    )
)
echo.

python -m pip install --upgrade pip --quiet %MIRROR%
pip install -e ".[gui,config]" --quiet %MIRROR%
if errorlevel 1 goto INSTALL_FAIL
echo Done.

:: -- 启动 GUI ---------------------------------------
:RUN
echo.
echo ============================================
echo   Starting epub-ruby GUI ...
echo ============================================
echo.
epub-ruby-gui
if errorlevel 1 goto GUI_ERROR
goto END

:: -- 错误处理 ---------------------------------------
:NO_PYTHON
echo [ERROR] Python not found. Install Python 3.8+ first.
echo https://www.python.org/downloads/
pause
exit /b 1

:VENV_FAIL
echo [ERROR] Failed to create virtual environment.
pause
exit /b 1

:ACTIVATE_FAIL
echo [ERROR] Failed to activate virtual environment.
echo Try deleting the .venv folder and running again.
pause
exit /b 1

:INSTALL_FAIL
echo [ERROR] Dependency installation failed. Check your network.
pause
exit /b 1

:GUI_ERROR
echo.
echo [WARNING] GUI exited with code %errorlevel%
pause

:END
