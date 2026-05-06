# 快速入门指南

快速上手epub-ruby的完整步骤。

## 📦 安装

### 系统要求

- Python 3.8+
- 200MB磁盘空间（包括依赖）

### 安装方式

**方式1：从PyPI安装（推荐）**

```bash
pip install epub-ruby
```

**方式2：安装GUI版本**

```bash
pip install epub-ruby[gui]
```

**方式3：安装开发版本**

```bash
git clone https://github.com/yourusername/epub-ruby.git
cd epub-ruby
pip install -e ".[dev,gui]"
```

## 🚀 基础使用

### 1️⃣ 最简单的用法（字典模式）

不需要任何配置，只需一条命令：

```bash
# 处理单个EPUB文件
epub-ruby my-book.epub

# 输出：my-book-ruby.epub（在当前目录）
```

**结果：** 所有汉字自动添加假名注音。

### 2️⃣ 指定输出目录

```bash
epub-ruby my-book.epub -d ~/processed-books/
```

### 3️⃣ 批处理整个文件夹

```bash
# 处理目录中所有EPUB文件
epub-ruby ~/my-library/
```

## 🤖 使用LLM增强（推荐用于复杂文本）

LLM（大语言模型）可以提供更准确的多音字标注。

### 步骤1：设置API密钥

选择一个LLM服务商：

**DeepSeek（最便宜）** ⭐推荐
```bash
export DEEPSEEK_API_KEY="sk-your-api-key-here"
# 获取密钥：https://platform.deepseek.com/
```

**OpenAI**
```bash
export OPENAI_API_KEY="sk-your-api-key-here"
# 获取密钥：https://platform.openai.com/
```

**Google Gemini（免费）**
```bash
export GEMINI_API_KEY="your-api-key-here"
# 获取密钥：https://aistudio.google.com/apikey
```

### 步骤2：使用LLM处理

```bash
epub-ruby my-book.epub --use-llm
```

就这么简单！工具会自动：
1. 分析所有文本
2. 调用LLM API生成准确的读音
3. 添加假名注音
4. 输出新的EPUB文件

## 🎛️ 常用选项

```bash
# 查看所有选项
epub-ruby --help

# 常见组合：

# 1. 处理后保存到特定目录
epub-ruby input.epub -d ~/output/

# 2. 使用LLM并指定模型
epub-ruby input.epub --use-llm --llm-model deepseek-v4-pro

# 3. 调整批处理大小（减少API调用）
epub-ruby input.epub --use-llm --batch-size 100

# 4. 限制并发（避免超过API限额）
epub-ruby input.epub --use-llm --max-concurrent 5

# 5. 使用多个API供应商（自动故障转移）
epub-ruby input.epub --use-llm \
  --api "deepseek:sk-xxx:deepseek-v4-flash:" \
  --api "gemini:your-key:gemini-2-flash-preview:"
```

## 💻 GUI应用

```bash
epub-ruby-gui
```

打开友好的图形界面，无需命令行。

## 📚 使用示例

### 示例1：处理你的小说收藏

```bash
# 批处理所有EPUB文件
epub-ruby ~/Library/eBooks/ -d ~/Library/eBooks-ruby/

# 结果：每本书都添加了假名注音
```

### 示例2：高精度处理（使用LLM）

```bash
# 用于重要文件或学习材料
epub-ruby important-book.epub --use-llm -d ~/output/

# LLM将提供上下文感知的准确读音
```

### 示例3：节省成本的多供应商处理

```bash
# 先用免费的Gemini，如果失败自动切换到DeepSeek
epub-ruby my-book.epub --use-llm \
  --api "gemini:gemini-key:gemini-2-flash-preview:" \
  --api "deepseek:sk-xxx:deepseek-v4-flash:"
```

### 示例4：从Python代码调用

```python
from pathlib import Path
from epub_ruby.core import EPUBHV

# 创建处理器
epub = EPUBHV(Path("my-book.epub"))

# 运行处理
output = epub.run(dest=Path("output"))

print(f"处理完成: {output}")
```

## ⚙️ 配置文件（可选）

如果需要保存常用设置，创建 `config.yaml`：

```yaml
llm:
  use_llm: true
  model: deepseek-v4-flash
  batch_size: 60
  max_concurrent: 3

processing:
  num_workers: 4
  output_dir: ~/processed-books

gui:
  theme: dark
  show_logs: true
```

使用配置文件：

```bash
epub-ruby my-book.epub  # 自动使用config.yaml
```

## 🔍 故障排除

### ❌ 问题：ImportError: No module named 'PySide6'

**解决方案：** 安装GUI依赖
```bash
pip install epub-ruby[gui]
```

### ❌ 问题：API quota exhausted

**解决方案：** 限制并发或使用另一个API
```bash
epub-ruby input.epub --use-llm --max-concurrent 3
# 或
epub-ruby input.epub --use-llm --api "gemini::gemini-2-flash-preview:"
```

### ❌ 问题：日语文本未正确标注

**解决方案：** 使用LLM获得更好的准确度
```bash
epub-ruby input.epub --use-llm
```

### ❌ 问题：看不到输出文件

检查你指定的输出目录是否存在：

```bash
# 创建输出目录
mkdir -p ~/my-output

# 指定输出
epub-ruby input.epub -d ~/my-output/
```

## 📊 性能提示

| 场景 | 建议 |
|------|------|
| 小文件 (<100KB) | 字典模式足够 |
| 大文件 (>10MB) | 使用LLM确保准确性 |
| 批处理100+本书 | 使用多个API供应商 |
| 免费API (Gemini) | 限制并发：--max-concurrent 5 |

## 📖 了解更多

- 完整文档：见 [README.md](README.md)
- 配置选项：见 `.env.example` 和 `config.example.yaml`
- 贡献代码：见 [CONTRIBUTING.md](CONTRIBUTING.md)
- 发现问题：打开 [Issue](https://github.com/8832two/epub_ruby/issues)

## ❓ 常见问题

**Q: 哪个LLM最便宜？**
A: DeepSeek价格最低，约$0.5-2.0每本书。

**Q: 可以离线使用吗？**
A: 字典模式可以完全离线。LLM模式需要网络和API。

**Q: 输出的EPUB在所有阅读器中都能工作吗？**
A: 是的，使用标准HTML ruby标签，所有现代阅读器都支持。

**Q: 可以自定义输出格式吗？**
A: 当前不支持，但计划在未来版本添加此功能。

---

**开始使用epub-ruby，让你的日语阅读之旅更轻松！** 📖✨

有任何问题？[提交Issue](https://github.com/8832two/epub_ruby/issues) 或查看 [完整文档](README.md)
