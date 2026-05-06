#!/usr/bin/env bash
# ──────────────────────────────────────────────
#  epub-ruby GUI 一键启动脚本 (macOS / Linux)
#  自动创建 venv、安装依赖、启动 GUI
# ──────────────────────────────────────────────
set -e

# 切换到脚本所在目录
cd "$(dirname "$0")"

VENV_DIR=".venv"
SENTINEL="$VENV_DIR/.deps_installed"

# ── 颜色定义 ──────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

info()  { echo -e "${CYAN}[信息]${NC} $*"; }
ok()    { echo -e "${GREEN}[完成]${NC} $*"; }
warn()  { echo -e "${YELLOW}[警告]${NC} $*"; }
err()   { echo -e "${RED}[错误]${NC} $*"; }

# ── 1. 检查 Python ────────────────────────────
if ! command -v python3 &>/dev/null; then
    err "未找到 python3，请先安装 Python 3.8+"
    err "下载地址: https://www.python.org/downloads/"
    exit 1
fi

PYVER=$(python3 --version 2>&1)
info "$PYVER"

# ── 2. 创建虚拟环境 ──────────────────────────
if [ ! -f "$VENV_DIR/bin/python" ]; then
    info "正在创建虚拟环境 ..."
    python3 -m venv "$VENV_DIR"
    # 新建 venv 后清除依赖标记
    rm -f "$SENTINEL"
    ok "虚拟环境已创建"
else
    info "虚拟环境已存在，跳过创建"
fi

# ── 3. 激活虚拟环境 ──────────────────────────
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

# ── 4. 安装依赖 ──────────────────────────────
if [ ! -f "$SENTINEL" ]; then
    info "正在安装依赖 (首次运行可能需要几分钟) ..."
    echo ""

    # 检测国内镜像，加速下载
    MIRROR=""
    if curl -s --connect-timeout 3 https://pypi.tuna.tsinghua.edu.cn >/dev/null 2>&1; then
        MIRROR="-i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn"
        info "使用清华 TUNA 镜像加速下载"
    elif curl -s --connect-timeout 3 https://mirrors.aliyun.com >/dev/null 2>&1; then
        MIRROR="-i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com"
        info "使用阿里云镜像加速下载"
    fi
    echo ""

    # 升级 pip
    python -m pip install --upgrade pip --quiet $MIRROR

    # 安装项目及 GUI 依赖
    pip install -e ".[gui,config]" $MIRROR

    # 写入标记文件
    echo "installed" > "$SENTINEL"
    echo ""
    ok "依赖安装完毕！"
else
    info "依赖已安装，跳过"
fi

echo ""
echo "════════════════════════════════════════════"
echo "  正在启动 epub-ruby GUI ..."
echo "════════════════════════════════════════════"
echo ""

# ── 5. 启动 GUI ──────────────────────────────
epub-ruby-gui
