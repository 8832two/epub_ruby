@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 打包 epub-ruby GUI

echo ================================================
echo   epub-ruby GUI - PyInstaller 打包脚本 (Windows)
echo ================================================
echo.

:: ── 检查 Python ──────────────────────────────────
where python >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 Python，请先安装 Python 3.8+
    pause
    exit /b 1
)
echo [信息] Python: 
python --version
echo.

:: ── 激活虚拟环境（如果存在） ────────────────────
if exist ".venv\Scripts\activate.bat" (
    call ".venv\Scripts\activate.bat"
    echo [信息] 已激活虚拟环境
) else (
    echo [警告] 未找到 .venv，将使用系统 Python
)
echo.

:: ── 安装打包依赖 ────────────────────────────────
echo [信息] 安装 PyInstaller...
pip install pyinstaller --quiet
if errorlevel 1 (
    echo [错误] PyInstaller 安装失败
    pause
    exit /b 1
)

:: ── 安装项目依赖（确保完整） ────────────────────
echo [信息] 安装项目依赖...
pip install -e ".[gui,config]" --quiet
if errorlevel 1 (
    echo [错误] 依赖安装失败
    pause
    exit /b 1
)
echo.

:: ── 清理旧构建 ──────────────────────────────────
if exist "build" rmdir /s /q "build"
if exist "dist\epub-ruby-gui" rmdir /s /q "dist\epub-ruby-gui"
echo [信息] 已清理旧构建文件
echo.

:: ── 开始打包 ────────────────────────────────────
echo [信息] 开始打包（可能需要 3-10 分钟，请耐心等待）...
echo.

pyinstaller epub_ruby_gui.spec --clean --noconfirm

if errorlevel 1 (
    echo.
    echo [错误] 打包失败！请检查上方错误信息。
    pause
    exit /b 1
)

:: ── 清理 onedir 残留文件夹（如果有） ──────────────
if exist "dist\epub-ruby-gui" rmdir /s /q "dist\epub-ruby-gui" 2>nul

echo.
echo ================================================
echo   打包完成！
echo   单文件: dist\epub-ruby-gui.exe
echo ================================================
echo.
echo 提示：这个 exe 是独立可执行文件，放到任何
echo       Windows 电脑都能直接运行。
echo.

:: ── 打开输出目录 ────────────────────────────────
start "" "dist"

pause
