# epub-ruby

> 给 EPUB 日文书籍自动添加振假名（ruby/furigana）的工具。  
> 支持纯词典模式 + DeepSeek LLM 上下文感知模式。

## 效果预览

```
原文：私は毎日日本語を勉強します
标注：<ruby>私<rt>わたし</rt></ruby>は<ruby>毎日<rt>まいにち</rt></ruby>
      <ruby>日本語<rt>にほんご</rt></ruby>を<ruby>勉強<rt>べんきょう</rt></ruby>します
```

---

## 功能

| 功能 | 说明 |
|---|---|
| 汉字 → 平假名 | kanji → hiragana，含送假名（食べる → たべる） |
| 片假名外来语 → 英文 | コンピュータ → computer |
| LLM 上下文消歧 | DeepSeek 根据整句判断多音字正确读音 |
| 批量并行 | 多个文件 / 多个 API batch 全部并行，无上限 |
| 断网容错 | 自动重试 + 指数退避，失败降级为 fugashi 读音 |

---

## 原理

```
EPUB (ZIP)
  → 解压 → 找到 .xhtml/.html 文件
    → fugashi (UniDic) 分词 → 识别需要注音的汉字词
      → [LLM 模式] 批量发送含 fugashi 读音的句子给 DeepSeek
        → LLM 只修正多音字错误，其余跳过（省 token）
      → 注入 <ruby><rt> 标签
  → 重新打包 → 输出 -ruby.epub
```

**LLM 模式的智能之处**：fugashi 先把所有词都标上读音，然后把整句 + 读音发给 DeepSeek："这些读音哪些是错的？只告诉我错的。"——LLM 不用从头算，只做 spot-check，大幅节省 token 和响应时间。

---

## 安装

```bash
git clone https://github.com/8832two/epub_ruby.git
cd epub_ruby

python -m venv .venv
source .venv/Scripts/activate   # Windows
# source .venv/bin/activate     # Linux / macOS

pip install -e .
```

---

## 使用方法

### 基本用法

```bash
# 纯词典模式（无需 API key，速度快但多音字可能不准）
epub-ruby book.epub

# LLM 模式（推荐，上下文感知，解决多音字问题）
epub-ruby book.epub --use-llm

# 指定输出目录
epub-ruby book.epub --use-llm -d ./output

# 批量处理整个目录
epub-ruby ./books --use-llm -d ./output
```

### LLM 选项

```bash
# 设置 API Key（二选一）
export DEEPSEEK_API_KEY="sk-..."
# 或
epub-ruby book.epub --use-llm --llm-api-key "sk-..."

# 切换模型（默认 deepseek-v4-flash 速度快成本低）
epub-ruby book.epub --use-llm --llm-model deepseek-v4-pro

# 自定义 API 地址（兼容 OpenAI 等）
epub-ruby book.epub --use-llm --llm-base-url "https://your-api.com"
```

### 完整参数

```
epub-ruby EPUB [选项]

位置参数:
  EPUB                  EPUB 文件或目录

可选参数:
  -d, --dest PATH       输出目录（默认当前目录）

LLM 选项:
  --use-llm             启用 DeepSeek API
  --llm-model MODEL     模型名（deepseek-v4-flash / deepseek-v4-pro）
  --llm-api-key KEY     API Key（默认读取 DEEPSEEK_API_KEY 环境变量）
  --llm-base-url URL    API 地址
```

输出文件：`原文件名-ruby.epub`

---

## 架构

```
epub_ruby/
├── cli.py          命令行解析
├── core.py         EPUB 解压/打包/并行调度
├── ruby.py         注音引擎（fugashi 分词 + <ruby> 标签注入）
├── llm_ruby.py     DeepSeek API 批量调用 + 重试 + 缓存
├── __init__.py
└── __main__.py
```

### 并行策略

- 所有 HTML 文件**同时并行**处理（无上限）
- 每个文件内的 API batch **同时并行**发出（无上限）
- 网络错误自动重试 3 次（1s → 2s → 4s 退避），失败降级为 fugashi 读音
- 实时输出带时间戳的进度日志

---

## 依赖

| 包 | 用途 |
|---|---|
| fugashi + unidic_lite | 日文分词 & 读音 |
| beautifulsoup4 + lxml | HTML 解析 & 标签注入 |
| openai | DeepSeek API 调用 |
| Python ≥ 3.8 | |

---

## 已知局限

- 纯词典模式下，多音字（如「人」→ ひと/にん/じん）准确率有限
- 极度依赖网络稳定性时建议使用 `deepseek-v4-flash`（快且便宜）
- 片假名外来语的英文释义来自 UniDic 词典，部分新词可能缺失

---

## 致谢

- [yihong0618/epubhv](https://github.com/yihong0618/epubhv) — 原始项目框架
- [Mumumu4/furigana4epub](https://github.com/Mumumu4/furigana4epub) — 注音引擎参考
- [fugashi](https://github.com/polm/fugashi) — 日文分词
- [DeepSeek](https://deepseek.com) — LLM API

## License

MIT
