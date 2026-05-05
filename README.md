# epub-ruby

> 给 EPUB 日文书籍自动添加振假名（ruby/furigana）注音的工具。

本项目基于 [yihong0618/epubhv](https://github.com/yihong0618/epubhv) 进行删改，是一个轻量级的 EPUB 日文注音处理工具。

## 功能

- **汉字 → 平假名**：自动为日文汉字标注平假名读音
- **片假名外来语 → 英文**：识别片假名外来语并标注对应的英文含义
- **批量处理**：支持单个 EPUB 文件或整个目录的批量注音

## 原理

1. 解压 EPUB（本质是 ZIP 压缩包）
2. 找到所有 HTML/XHTML 内容文件
3. 使用 [fugashi](https://github.com/polm/fugashi)（MeCab 的 Python 封装）+ unidic_lite 词典进行日文分词和读音分析
4. 在 HTML 中注入 `<ruby>` / `<rt>` / `<rp>` 标签
5. 重新打包为新的 EPUB 文件

## 安装

```bash
# 克隆仓库
git clone https://github.com/8832two/epub_ruby.git
cd epub_ruby

# 创建虚拟环境并安装
python -m venv .venv
source .venv/Scripts/activate  # Windows
# 或 source .venv/bin/activate  # Linux/macOS

pip install -e .
```

## 使用方法

```bash
# 处理单个 EPUB 文件（输出到当前目录）
epub-ruby book.epub

# 指定输出目录
epub-ruby book.epub -d ./output

# 批量处理整个目录
epub-ruby ./books -d ./annotated

# 或以模块方式运行
python -m epub_ruby book.epub
```

输出文件命名规则：`原文件名-ruby.epub`

## 依赖

- Python >= 3.8
- [beautifulsoup4](https://pypi.org/project/beautifulsoup4/) — HTML 解析
- [lxml](https://pypi.org/project/lxml/) — XML/HTML 引擎
- [fugashi](https://pypi.org/project/fugashi/) — MeCab 分词器封装
- [unidic_lite](https://pypi.org/project/unidic-lite/) — 日文词典

## 已知局限

当前使用基于词典的分词器（MeCab/unidic）进行读音推断，对于**多音字**（同一个汉字有多种读法）的注音准确率有限。例如「行く」（いく）和「旅行」（りょこう）中的「行」读音完全不同，分词器在复杂上下文中可能出错。

这是本项目的已知问题，**后续计划引入大模型（LLM）来优化多音字的注音准确性**。

## 项目结构

```
epub_ruby/
├── pyproject.toml          # 项目配置
├── README.md
└── epub_ruby/
    ├── __init__.py         # 包入口
    ├── __main__.py         # python -m 入口
    ├── cli.py              # 命令行解析
    ├── core.py             # EPUB 解压/打包/调度
    └── ruby.py             # 注音引擎（分词 + ruby 注入）
```

## 致谢

- [yihong0618/epubhv](https://github.com/yihong0618/epubhv) — 原始项目
- [Mumumu4/furigana4epub](https://github.com/Mumumu4/furigana4epub) — 注音引擎参考
- [fugashi](https://github.com/polm/fugashi) — 日文分词器

## License

MIT
