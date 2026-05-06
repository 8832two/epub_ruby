# Changelog

所有对epub-ruby项目的重大更改都将记录在此文件中。

该项目遵循 [语义化版本控制](https://semver.org/lang/zh-CN/)。

## [0.2.0] - 2026-05-06

### 新增

- ✨ 配置系统 (config.py) - 支持YAML配置文件和环境变量
- ✨ 统一日志系统 (logger.py) - 彩色控制台输出和日志文件轮转
- ✨ 自定义异常类 (exceptions.py) - 更好的错误处理和诊断
- ✨ 单元测试框架 - pytest配置和基本测试
- ✨ 代码规范配置 - .editorconfig, pyproject.toml工具配置
- ✨ 完整的README文档 - 详细的使用指南和API文档
- ✨ 贡献指南 (CONTRIBUTING.md) - 开发者入门指南

### 改进

- 🔧 改进core.py的错误处理 - 添加详细的验证和异常处理
- 🔧 增强__init__方法的输入验证
- 🔧 添加更详细的日志记录便于调试
- 📝 更新pyproject.toml - 更完整的元数据和可选依赖
- 🔄 代码结构优化 - 更好的模块化和可维护性

### 文档

- 📚 编写完整的README.md - 包括快速开始、高级用法、故障排除
- 📚 添加API文档和使用示例
- 📚 创建配置文件示例和环境变量指南
- 📚 编写贡献指南

## [0.1.0] - 初始发布

### 功能

- 基础EPUB处理和假名标注
- 两种模式：字典模式和LLM增强模式
- 支持多个LLM提供商（DeepSeek, OpenAI, Gemini）
- API连接池和自动故障转移
- 命令行接口
- 基础GUI应用

### 已知限制

- GUI模块需要重构（过长）
- 缺少完整的文档
- 缺少单元测试

---

## 版本说明

### 语义化版本

格式: `MAJOR.MINOR.PATCH`

- **MAJOR** - 破坏性API变更时增加
- **MINOR** - 添加新功能（向后兼容）时增加
- **PATCH** - 修复bug或小改进时增加

### 发布周期

- 稳定版本：根据功能完成度，约每2-3个月发布一次
- 测试版本：在GitHub Releases中发布预发布版本

---

## 更新指南

### 从0.1.x升级到0.2.0

无破坏性变更。现有脚本应继续工作。

**新增选项：**
- 支持配置文件：创建 `config.yaml` 并使用 `--config` 选项
- 支持环境变量：详见 `.env.example`
- 改进的日志：自动保存到 `~/.epub_ruby/epub_ruby.log`

### 推荐更新

```bash
# 更新到最新版本
pip install --upgrade epub-ruby

# 或从源代码安装开发版本
git clone https://github.com/yourusername/epub-ruby.git
cd epub-ruby
pip install -e .
```

---

## 计划中的功能（未来版本）

### 0.3.0 (计划中)

- 性能优化：增量处理和缓存改进
- GUI重构：模块化和主题支持
- 更多语言支持（中文、韩文等）
- Web应用版本

### 1.0.0 (长期)

- 完整的API文档
- 插件系统
- 高级定制选项
- 企业级支持

---

## 报告问题

发现bug或问题？请提交 [Issue](https://github.com/8832two/epub_ruby/issues)

包含以下信息：
- 你的操作系统和Python版本
- 问题的清晰描述
- 复现步骤
- 错误日志（~/.epub_ruby/errors.log）
- 如果可能，提供测试文件

---

最后更新：2026年5月6日
