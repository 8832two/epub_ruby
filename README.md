<p align="center">
  <h1 align="center">📖 epub-ruby</h1>
  <p align="center"><b>为 EPUB 电子书自动添加日语振假名（ふりがな）</b></p>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.8+-blue" alt="Python">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="License">
  <img src="https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey" alt="Platform">
</p>

---

##  快速开始

### Windows 用户（推荐）

1.  **[下载 epub-ruby-gui.exe](../../releases/latest/download/epub-ruby-gui.exe)**
2. 双击运行，GUI 界面立即可用

> 单文件独立运行，无需安装 Python 或任何依赖。

### macOS / Linux 用户

```bash
git clone https://github.com/8832two/epub_ruby.git
cd epub_ruby
./run_gui.sh          # 自动安装依赖并启动 GUI（首次约 1-2 分钟）
```

---

## ✨ 功能

-  **字典模式** — MeCab / UniDic 自动标注汉字读音，无需联网
-  **LLM 增强** — 支持 DeepSeek、OpenAI、Gemini，精准处理多音字
-  **图形界面** — 拖拽 EPUB 即可处理，支持批量
-  **并发加速** — 多线程 + API 并发池
-  **样式保留** — 完整保留原书 CSS、图片、排版

---

##  GUI 使用

1. 将 `.epub` 文件拖入窗口（或 **文件 → 打开**）
2. （可选）在 **API 管理** 中配置 LLM
3. 点击 **开始处理**
4. 输出为 `原文件名-ruby.epub`

### 字典模式 vs LLM 模式

| | 字典模式 | LLM 模式 |
|---|---|---|
| 需要网络 | ❌ | ✅ |
| 需要 API Key | ❌ | ✅ |
| 准确度 | 较高 | 更高（处理多音字） |

---

##  LLM 配置

在 GUI 的 **API 管理** 页面添加 API Key，或设置环境变量：

| 服务商 | 环境变量 | 获取地址 |
|--------|----------|----------|
| DeepSeek（推荐） | `DEEPSEEK_API_KEY` | https://platform.deepseek.com |
| OpenAI | `OPENAI_API_KEY` | https://platform.openai.com |
| Gemini | `GEMINI_API_KEY` | https://aistudio.google.com/apikey |

---

##  命令行（高级用户）

```bash
pip install -e ".[cli,config]"

epub-ruby my-book.epub              # 字典模式
epub-ruby my-book.epub --llm        # LLM 模式
epub-ruby ./my-library/             # 批量处理
```

---

##  自行打包

```bash
build_gui.bat           # Windows
./build_gui.sh          # macOS / Linux
```

输出 `dist/epub-ruby-gui.exe`，单文件独立运行。

---

##  项目结构

```
epub_ruby/
├── epub_ruby_gui.spec    # PyInstaller 打包配置
├── build_gui.bat / .sh   # 打包脚本
├── run_gui.bat / .sh     # 源码启动脚本
├── epub_ruby/            # 源代码
│   ├── gui.py            # 图形界面
│   ├── cli.py            # 命令行入口
│   ├── core.py           # 核心处理
│   ├── ruby.py           # 振假名注入
│   ├── llm_ruby.py       # LLM 注音
│   └── api_pool.py       # API 并发池
├── config.example.yaml   # 配置文件模板
└── README.md
```

---

## 致谢

本项目基于 [github.com/yihong0618/epubhv](https://github.com/yihong0618/epubhv) 修改而来，感谢原作者的杰出工作。

---
