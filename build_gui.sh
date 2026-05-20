#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════
#  epub-ruby GUI - PyInstaller 打包脚本 (macOS / Linux)
# ═══════════════════════════════════════════════════════════════════
set -e

cd "$(dirname "$0")"

RED='\033[0;31m'
GREEN='\033[0;32m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${CYAN}[信息]${NC} $*"; }
ok()    { echo -e "${GREEN}[完成]${NC} $*"; }
err()   { echo -e "${RED}[错误]${NC} $*"; }

echo "================================================"
echo "  epub-ruby GUI - PyInstaller 打包脚本"
echo "================================================"
echo ""

# ── 检查 Python ──────────────────────────────────
if ! command -v python3 &>/dev/null; then
    err "未找到 python3，请先安装 Python 3.8+"
    exit 1
fi
info "Python: $(python3 --version)"

# ── 激活虚拟环境 ──────────────────────────────────
if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
    info "已激活虚拟环境"
else
    info "使用系统 Python（建议先创建 venv）"
fi
echo ""

# ── 安装打包依赖 ──────────────────────────────────
info "安装 PyInstaller..."
pip install pyinstaller --quiet

info "安装项目依赖..."
pip install -e ".[gui,config]" --quiet
echo ""

# ── 清理旧构建 ──────────────────────────────────
rm -rf build dist/epub-ruby-gui
info "已清理旧构建文件"
echo ""

# ── 开始打包 ────────────────────────────────────
info "开始打包（可能需要 3-10 分钟）..."
echo ""

pyinstaller epub_ruby_gui.spec --clean --noconfirm

# ── 清理 onedir 残留文件夹（如果有） ──────────────
rm -rf dist/epub-ruby-gui

echo ""
ok "打包完成！"
echo "  单文件: dist/epub-ruby-gui"
echo ""
echo "提示：这个可执行文件是独立的，放到任何"
echo "      电脑都能直接运行。"
echo ""

# ── 打开输出目录 ────────────────────────────────
if [[ "$OSTYPE" == "darwin"* ]]; then
    open dist/epub-ruby-gui
elif [[ "$OSTYPE" == "linux-gnu"* ]]; then
    xdg-open dist/epub-ruby-gui &>/dev/null || true
fi
