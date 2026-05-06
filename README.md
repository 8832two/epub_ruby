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

## ✨ 功能

- 🔤 **字典模式** — 基于 MeCab / UniDic 自动标注汉字读音，无需联网
- 🤖 **LLM 增强** — 支持 DeepSeek、OpenAI、Gemini 等大模型，精准处理多音字
- 🖥️ **图形界面** — PySide6 打造的现代化 GUI，拖拽即可处理
- 📚 **批量处理** — 一次处理整个文件夹的所有 EPUB
- ⚡ **并发加速** — 多线程 + API 并发池，大幅缩短处理时间
- 🎨 **样式保留** — 完整保留原书 CSS、图片、排版

---

## 📋 环境要求

| 项目 | 说明 |
|------|------|
| Python | ≥ 3.8 |
| 操作系统 | Windows / macOS / Linux |
| 磁盘空间 | ~300 MB（含 UniDic 词典） |

---

## 🚀 快速开始

### 方式一：一键启动（推荐）

无需手动安装任何东西，下载项目后双击脚本即可：

| 系统 | 脚本 |
|------|------|
| Windows | 双击 `run_gui.bat` |
| macOS / Linux | 终端运行 `./run_gui.sh` |

脚本会自动完成：
1. 创建 Python 虚拟环境
2. 安装全部依赖（首次约 1-2 分钟）
3. 启动 GUI

> 之后再次运行，检测到环境已就绪，直接秒开。

### 方式二：源码安装

```bash
git clone https://github.com/8832two/epub_ruby.git
cd epub_ruby
pip install -e ".[gui,config]"
```

安装后可通过命令行使用：

```bash
# 启动 GUI
epub-ruby-gui

# 或命令行处理
epub-ruby my-book.epub
```

---

## 🖥️ 使用 GUI

```bash
epub-ruby-gui
```

1. 点击 **文件 → 打开** 选择一个 `.epub` 文件（或直接拖入窗口）
2. 在 **API 管理** 中配置 LLM（可选，不配则使用纯字典模式）
3. 点击 **开始处理**
4. 输出文件自动保存为 `原文件名-ruby.epub`

---

## ⌨️ 命令行使用

```bash
# 处理单个文件（字典模式）
epub-ruby my-book.epub

# 指定输出目录
epub-ruby my-book.epub -d ./output/

# 批量处理整个目录
epub-ruby ./my-library/

# 使用 LLM 模式（需先设置 API Key）
epub-ruby my-book.epub --llm
```

---

## 🤖 LLM 配置

在 GUI 的 **API 管理** 页面添加 API，或设置环境变量：

| 服务商 | 环境变量 |
|--------|----------|
| DeepSeek（推荐，便宜） | `DEEPSEEK_API_KEY` |
| OpenAI | `OPENAI_API_KEY` |
| Gemini | `GEMINI_API_KEY` |

> DeepSeek 获取密钥：https://platform.deepseek.com/

也可通过 `config.yaml` 配置，参考 `config.example.yaml`。

---

## 📁 项目结构

```
epub_ruby/
├── run_gui.bat          # Windows 一键启动
├── run_gui.sh           # macOS / Linux 一键启动
├── epub_ruby/
│   ├── gui.py           # 图形界面
│   ├── cli.py           # 命令行入口
│   ├── core.py          # 核心处理逻辑
│   ├── ruby.py          # 振假名注入
│   ├── llm_ruby.py      # LLM 注音
│   ├── api_pool.py      # API 并发池
│   └── config.py        # 配置管理
├── config.example.yaml  # 配置文件模板
├── pyproject.toml
└── README.md
```

---

## 🔧 故障排除

| 问题 | 解决方案 |
|------|----------|
| `ModuleNotFoundError: PySide6` | 安装 GUI 依赖：`pip install -e ".[gui]"` |
| API 调用失败 | 检查 API Key 是否正确，网络是否可访问 |
| 处理速度慢 | 调大 `batch_size` 或增加 `max_concurrent` |
| UniDic 下载失败 | 手动运行 `python -m unidic download` |

错误日志位于 `~/.epub_ruby/errors.log`。

---

## 📄 许可证

MIT License
