# 贡献指南

感谢你有兴趣为epub-ruby做出贡献！本指南将帮助你理解我们的开发流程。

## 🤝 贡献方式

### 报告Bug

- 使用 GitHub Issues 提交bug报告
- 清楚地描述问题和复现步骤
- 提供以下信息：
  - Python版本
  - 操作系统
  - 使用的EPUB文件信息
  - 完整的错误堆栈跟踪
  - 日志文件位置：`~/.epub_ruby/errors.log`

### 提出功能建议

- 在 GitHub Discussions 中讨论新功能
- 解释为什么需要此功能
- 提供使用场景示例

### 提交代码更改

1. **Fork本仓库**

```bash
git clone https://github.com/yourusername/epub-ruby.git
cd epub-ruby
```

2. **创建虚拟环境和安装依赖**

```bash
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# 安装开发依赖
pip install -e ".[dev,gui,config]"
```

3. **创建特性分支**

```bash
git checkout -b feature/amazing-feature
```

4. **编写代码**

- 遵循现有代码风格
- 添加单元测试
- 更新文档
- 确保所有测试通过

5. **运行测试和检查**

```bash
# 运行测试
pytest

# 代码格式化
black epub_ruby/ tests/

# 代码排序
isort epub_ruby/ tests/

# 静态检查
pylint epub_ruby/
```

6. **提交更改**

```bash
git add .
git commit -m "Add amazing feature"
```

使用清晰、简洁的提交信息。格式：

```
<type>: <subject>

<body>

<footer>
```

类型：
- `feat`: 新功能
- `fix`: Bug修复
- `docs`: 文档更新
- `style`: 代码格式化
- `refactor`: 代码重构
- `perf`: 性能优化
- `test`: 测试相关
- `chore`: 构建、依赖等

示例：
```
feat: add batch processing for multiple EPUBs

Allows users to process entire directories of EPUB files
in one command with progress tracking.

Fixes #123
```

7. **推送到Fork**

```bash
git push origin feature/amazing-feature
```

8. **打开Pull Request**

- 清晰地描述您的更改
- 引用相关的issue
- 提供测试结果证明

## 📋 代码标准

### Python代码风格

- 遵循 [PEP 8](https://www.python.org/dev/peps/pep-0008/)
- 使用 Black 进行格式化（line length = 100）
- 使用 isort 组织导入
- 添加类型提示（Python 3.8+）

### 文档标准

- 使用 Google 风格的docstrings
- 添加使用示例
- 记录异常

示例：
```python
def process_epub(
    file_path: Path,
    use_llm: bool = False,
) -> Path:
    \"\"\"Process an EPUB file and add furigana annotations.

    Args:
        file_path: Path to the EPUB file to process.
        use_llm: Whether to use LLM for better accuracy.

    Returns:
        Path to the processed EPUB file (-ruby.epub).

    Raises:
        EPUBInvalidError: If the file is not a valid EPUB.
        EPUBExtractionError: If extraction fails.

    Examples:
        >>> from pathlib import Path
        >>> from epub_ruby import EPUBHV
        >>> epub = EPUBHV(Path('novel.epub'))
        >>> output = epub.run()
        >>> print(output)
        Path('novel-ruby.epub')
    \"\"\"
```

### 测试标准

- 为新功能编写测试
- 保持测试简单、独立、快速
- 使用有意义的测试名称

```python
def test_katakana_to_hiragana_conversion():
    \"\"\"Test converting katakana to hiragana.\"\"\"
    result = katakana_to_hiragana(\"カタカナ\")
    assert result == \"かたかな\"
```

## 🔍 审查流程

1. 自动检查（GitHub Actions）
   - 单元测试
   - 代码格式检查
   - 类型检查

2. 人工审查
   - 代码质量
   - 文档完整性
   - 性能影响

3. 合并
   - 通过所有检查后可以合并

## 📚 项目结构

```
epub_ruby/
├── __init__.py          # 包初始化
├── __main__.py          # 模块执行
├── cli.py              # 命令行接口
├── core.py             # 主要处理逻辑
├── ruby.py             # 假名生成引擎
├── llm_ruby.py         # LLM集成
├── api_pool.py         # API管理
├── gui.py              # GUI应用
├── config.py           # 配置管理
├── logger.py           # 日志系统
├── exceptions.py       # 自定义异常
└── __init__.py

tests/
├── __init__.py
├── test_config.py
├── test_exceptions.py
├── test_ruby.py
└── test_core.py

docs/                   # 文档
examples/               # 使用示例
```

## 🚀 发布流程

1. 更新版本号（在 `pyproject.toml`）
2. 更新 `CHANGELOG.md`
3. 创建git tag：`git tag v0.2.0`
4. 推送到主仓库
5. GitHub Actions 自动发布到 PyPI

## ❓ 常见问题

**Q: 我应该在哪里添加新功能？**
A: 根据功能类型：
- CLI命令 → `cli.py`
- 核心处理逻辑 → `core.py`
- UI组件 → `gui.py`
- 配置选项 → `config.py`

**Q: 如何运行特定的测试？**
A: 
```bash
pytest tests/test_ruby.py::test_katakana_to_hiragana_conversion
```

**Q: 如何生成覆盖率报告？**
A:
```bash
pytest --cov=epub_ruby --cov-report=html
```

## 📞 获取帮助

- 📖 查看 [README.md](README.md) 了解项目概况
- 🐛 检查现有 [Issues](https://github.com/8832two/epub_ruby/issues)
- 💬 在 [Discussions](https://github.com/8832two/epub_ruby/discussions) 中提问
- 📧 联系维护者

## 📄 许可证

通过提交代码，你同意你的贡献将在 MIT 许可证下发布。

---

感谢你的贡献！让我们一起让epub-ruby更好！ 🎉
