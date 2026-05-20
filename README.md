<p align="center">
  <h1 align="center">📖 epub-ruby</h1>
  <p align="center"><b>为 EPUB 日文电子书自动添加振假名（ふりがな）</b></p>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.8+-blue" alt="Python">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="License">
  <img src="https://img.shields.io/badge/version-0.3.0-orange" alt="Version">
  <img src="https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey" alt="Platform">
</p>

---

## 📖 简介

**epub-ruby** 是一个自动化工具，为 EPUB 格式的日文电子书批量添加**振假名（ruby / furigana）**注音标注。它会在汉字上方插入 `<ruby>` 标签，让阅读器能够显示假名注音，大幅降低日语阅读门槛。

项目支持两种工作模式：

| 模式 | 原理 | 优点 | 适用场景 |
|------|------|------|----------|
| **字典模式** | 基于 fugashi (UniDic) 自动分词并查询读音 | 完全离线、速度快、免费 | 快速批量处理 |
| **LLM 模式** | 借助大语言模型（DeepSeek / OpenAI / Gemini）根据上下文判断读音 | 多音字更准确、片假名外来语附英文释义 | 追求最高准确度 |

除此之外，LLM 模式下还会自动为片假名外来语（如 `コンピュータ`）添加对应英文释义（`computer`）。

---

## 🚀 快速开始

### Windows 用户（推荐）

1. 从 [Releases](../../releases) 下载 `epub-ruby-gui.exe`
2. 双击运行，即开即用

> 单文件绿色版，无需安装 Python 或任何运行环境。

### macOS / Linux 用户

```bash
git clone https://github.com/8832two/epub_ruby.git
cd epub_ruby
./run_gui.sh          # 自动创建虚拟环境、安装依赖并启动 GUI
```

---

## 🖥️ 图形界面 (GUI)

基于 PySide6 构建的现代化图形界面，所有操作均可通过菜单栏与页面切换完成。

### 四大页面

| 页面 | 功能 |
|------|------|
| **输入输出** | 拖放或浏览选择 EPUB 文件 / 文件夹，指定输出目录 |
| **处理** | 选择「普通模式」或「LLM 模式」，调整批次大小与并发参数 |
| **API 管理** | 管理多个 LLM API 提供商，按优先级排列，额度用尽自动故障切换 |
| **帮助** | 使用说明与项目信息 |

### 操作流程

1. 在 **输入输出** 页拖入 `.epub` 文件或文件夹
2. 在 **处理** 页选择模式标签（普通 / LLM）
3. （LLM 模式）在 **API 管理** 页配置 API Key
4. 点击 **▶ 开始处理**
5. 输出文件为 `原文件名-ruby.epub`

### LLM 参数说明

| 参数 | 默认值 | 说明 |
|------|--------|------|
| 批次大小 | 200 句/次 | 每次 API 调用发送的句子数，越大 token 利用率越高 |
| 并发数 | 10 | 同时进行的 API 调用数。Gemini 免费层建议 5–10；0 = 不限 |
| 最大 Token | 384,000 | 单次 API 调用的输出上限，批次大时建议调大以防截断 |

---

## ⌨️ 命令行 (CLI)

适合高级用户、脚本集成与服务器环境。

```bash
# 安装
pip install -e ".[cli,config]"

# 字典模式（离线）
epub-ruby my-book.epub

# LLM 模式（单 API）
epub-ruby my-book.epub --use-llm

# LLM 模式（多 API 池，自动故障切换）
epub-ruby my-book.epub --use-llm \
  --api "deepseek:sk-xxx:deepseek-v4-flash:" \
  --api "gemini::gemini-2.5-flash:"

# 批量处理目录
epub-ruby ./my-library/ --use-llm --api "deepseek:sk-xxx:deepseek-v4-flash:"

# 自定义参数
epub-ruby my-book.epub --use-llm \
  --batch-size 300 \
  --max-concurrent 15 \
  --llm-max-tokens 65536 \
  -d ./output
```

### CLI 参数一览

| 参数 | 说明 |
|------|------|
| `epub` | EPUB 文件或目录路径 |
| `-d, --dest` | 输出目录（默认当前目录） |
| `--use-llm` | 启用 LLM 模式 |
| `--api provider:key:model:url` | 添加 API 提供商（可重复指定） |
| `--batch-size` | 每次 API 调用的句子数（默认 200） |
| `--max-concurrent` | 最大并发 API 调用数（默认 0 = 不限） |
| `--llm-max-tokens` | 单次 API 输出上限（默认 384000） |
| `--llm-model` | 模型名（单 API 模式） |
| `--llm-api-key` | API Key（单 API 模式） |
| `--llm-base-url` | API 地址（单 API 模式） |

---

## 🔑 LLM API 配置

### 支持的提供商

| 提供商 | 环境变量 | Key 获取地址 |
|--------|----------|-------------|
| **DeepSeek**（推荐） | `DEEPSEEK_API_KEY` | [platform.deepseek.com](https://platform.deepseek.com) |
| **OpenAI** | `OPENAI_API_KEY` | [platform.openai.com](https://platform.openai.com) |
| **Gemini** | `GEMINI_API_KEY` | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) |

### API 池机制

支持同时配置**多个 API 提供商**，按表格中的优先级依次调用：

- 当前提供商额度用尽（返回 429 等错误）时，**自动切换到下一个**
- 被暂停的提供商在 60 秒后自动恢复重试
- 所有提供商均耗尽时，抛出明确错误提示

> 适合免费层额度有限的场景（如 Gemini 免费层 15 RPM），可以搭配多个账号交替使用。

### 磁盘缓存

LLM 的注音结果会自动缓存至 `~/.epub_ruby/llm_cache/readings.json`。同一句子重复处理时直接读取缓存，**不会消耗 API 额度**。

---

## 🏗️ 项目架构

```
epub_ruby/
├── epub_ruby/                # Python 包
│   ├── __init__.py           # 包入口，导出 EPUBHV
│   ├── __main__.py           # python -m epub_ruby
│   ├── core.py               # 核心处理引擎：解包→注音→打包
│   ├── ruby.py               # 振假名注入引擎（RubySoup）
│   ├── llm_ruby.py           # LLM 注音模块（批量 API + 缓存 + JSON 解析）
│   ├── api_pool.py           # 多 API 提供商池（故障切换 + 限流恢复）
│   ├── katakana_english.py   # 片假名→英文释义（基于 UniDic lemma）
│   ├── cli.py                # 命令行接口
│   ├── gui.py                # PySide6 图形界面
│   ├── config.py             # 配置管理（环境变量 / YAML）
│   ├── exceptions.py         # 自定义异常
│   └── logger.py             # 统一日志系统
├── pyproject.toml            # 项目元数据与依赖
├── epub_ruby_gui.spec        # PyInstaller 打包配置
├── build_gui.bat / .sh       # 一键打包脚本
├── run_gui.bat / .sh         # 一键启动脚本
└── README.md
```

### 核心模块关系

```
CLI (cli.py)  /  GUI (gui.py)
        │
        ▼
   EPUBHV (core.py)         ← 主控制器：解包 → 注音 → 打包
        │
        ├── RubySoup (ruby.py)      ← HTML 解析与 <ruby> 标签注入
        │       │
        │       ├── fugashi/UniDic   ← 字典模式：分词 + 读音查询
        │       └── LLMRubyReader    ← LLM 模式：上下文感知注音
        │               │
        │               └── APIPool (api_pool.py)  ← 多 API 故障切换池
        │
        └── katakana_english.py     ← 片假名外来语 → 英文释义
```

### 处理流程

```
.epub 输入
  → 解压到临时目录
  → 收集所有 HTML/XHTML 文件
  → 两阶段处理:
       [字典模式] 逐文件并行分词注入 <ruby>
       [LLM 模式]  Phase 1: 收集所有句子 → Phase 2: 批量 API 调用
                  → Phase 3: 并行注入 <ruby>
  → 文本完整性校验（防止注音损坏原文）
  → 重新打包为 .epub
  → 清理临时文件
  → 输出 {原名}-ruby.epub
```

---

## 📦 自行打包

```bash
# Windows
build_gui.bat

# macOS / Linux
./build_gui.sh
```

输出 `dist/epub-ruby-gui.exe`（或对应平台可执行文件）。

---

## 🛠️ 开发

```bash
# 克隆项目
git clone https://github.com/8832two/epub_ruby.git
cd epub_ruby

# 安装开发依赖
pip install -e ".[gui,config,dev,build]"

# 运行测试
pytest

# 代码格式化
black epub_ruby/
```

---

## 🙏 致谢

本项目基于 [yihong0618/epubhv](https://github.com/yihong0618/epubhv) 修改而来，感谢原作者提供的出色基础框架。

核心依赖：

- [fugashi](https://github.com/polm/fugashi) + [UniDic](https://unidic.ninjal.ac.jp/) — 日语分词与词典
- [BeautifulSoup4](https://www.crummy.com/software/BeautifulSoup/) + [lxml](https://lxml.de/) — HTML 解析
- [PySide6](https://wiki.qt.io/Qt_for_Python) — 图形界面
- [OpenAI SDK](https://github.com/openai/openai-python) / [google-genai](https://github.com/google-gemini/generative-ai-python) — LLM API

---

## 📄 License

MIT License. 详见项目根目录 LICENSE 文件。

---
