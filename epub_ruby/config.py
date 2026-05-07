"""
Configuration management for epub-ruby.

Supports environment variables and config files (.env, config.yaml).
"""

from __future__ import annotations

import os
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, Dict, Any

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore


@dataclass
class Config:
    """Global configuration container."""

    # LLM settings
    use_llm: bool = False
    llm_model: str = "deepseek-chat"
    llm_api_key: Optional[str] = None
    llm_base_url: str = "https://api.deepseek.com"
    llm_batch_size: int = 200
    llm_max_concurrent: int = 0

    # Processing
    num_workers: int = 0  # 0 = auto
    output_dir: Optional[Path] = None

    # GUI
    gui_theme: str = "light"  # "light" or "dark"
    gui_show_logs: bool = True
    gui_remember_settings: bool = True

    @classmethod
    def from_env(cls) -> Config:
        """Load configuration from environment variables."""
        return cls(
            use_llm=os.getenv("EPUB_RUBY_USE_LLM", "false").lower() == "true",
            llm_model=os.getenv("EPUB_RUBY_LLM_MODEL", "deepseek-chat"),
            llm_api_key=os.getenv("DEEPSEEK_API_KEY"),
            llm_base_url=os.getenv(
                "EPUB_RUBY_LLM_BASE_URL",
                "https://api.deepseek.com"
            ),
            llm_batch_size=int(os.getenv("EPUB_RUBY_BATCH_SIZE", "200")),
            llm_max_concurrent=int(os.getenv("EPUB_RUBY_MAX_CONCURRENT", "0")),
            output_dir=Path(os.getenv("EPUB_RUBY_OUTPUT_DIR")) if
            os.getenv("EPUB_RUBY_OUTPUT_DIR") else None,
            gui_theme=os.getenv("EPUB_RUBY_GUI_THEME", "light"),
        )

    @classmethod
    def from_file(cls, config_path: Path | str) -> Config:
        """Load configuration from YAML file.

        Example config.yaml:
            llm:
              use_llm: true
              model: deepseek-v4-flash
              batch_size: 60
            processing:
              num_workers: 4
              output_dir: /path/to/output
            gui:
              theme: dark
        """
        if yaml is None:
            raise ImportError(
                "PyYAML not installed. "
                "Install with: pip install pyyaml"
            )

        config_path = Path(config_path)
        if not config_path.exists():
            raise FileNotFoundError(f"Config file not found: {config_path}")

        with open(config_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        llm_cfg = data.get("llm", {})
        proc_cfg = data.get("processing", {})
        gui_cfg = data.get("gui", {})

        return cls(
            use_llm=llm_cfg.get("use_llm", False),
            llm_model=llm_cfg.get("model", "deepseek-chat"),
            llm_api_key=llm_cfg.get("api_key"),
            llm_base_url=llm_cfg.get("base_url", "https://api.deepseek.com"),
            llm_batch_size=llm_cfg.get("batch_size", 200),
            llm_max_concurrent=llm_cfg.get("max_concurrent", 0),
            num_workers=proc_cfg.get("num_workers", 0),
            output_dir=Path(proc_cfg.get("output_dir"))
            if proc_cfg.get("output_dir") else None,
            gui_theme=gui_cfg.get("theme", "light"),
            gui_show_logs=gui_cfg.get("show_logs", True),
            gui_remember_settings=gui_cfg.get("remember_settings", True),
        )

    @classmethod
    def from_env_and_file(
        cls,
        config_path: Optional[Path | str] = None
    ) -> Config:
        """Load config from file first, then override with env vars."""
        # Start with file config
        if config_path and Path(config_path).exists():
            cfg = cls.from_file(config_path)
        else:
            cfg = cls()

        # Override with environment variables
        env_cfg = cls.from_env()

        # Merge: use env values if they differ from defaults
        if os.getenv("EPUB_RUBY_USE_LLM"):
            cfg.use_llm = env_cfg.use_llm
        if os.getenv("EPUB_RUBY_LLM_MODEL"):
            cfg.llm_model = env_cfg.llm_model
        if os.getenv("DEEPSEEK_API_KEY"):
            cfg.llm_api_key = env_cfg.llm_api_key
        if os.getenv("EPUB_RUBY_LLM_BASE_URL"):
            cfg.llm_base_url = env_cfg.llm_base_url
        if os.getenv("EPUB_RUBY_BATCH_SIZE"):
            cfg.llm_batch_size = env_cfg.llm_batch_size
        if os.getenv("EPUB_RUBY_MAX_CONCURRENT"):
            cfg.llm_max_concurrent = env_cfg.llm_max_concurrent
        if os.getenv("EPUB_RUBY_OUTPUT_DIR"):
            cfg.output_dir = env_cfg.output_dir
        if os.getenv("EPUB_RUBY_GUI_THEME"):
            cfg.gui_theme = env_cfg.gui_theme

        return cfg

    def to_dict(self) -> Dict[str, Any]:
        """Convert config to dictionary (for serialization)."""
        return {
            "llm": {
                "use_llm": self.use_llm,
                "model": self.llm_model,
                "api_key": self.llm_api_key,
                "base_url": self.llm_base_url,
                "batch_size": self.llm_batch_size,
                "max_concurrent": self.llm_max_concurrent,
            },
            "processing": {
                "num_workers": self.num_workers,
                "output_dir": str(self.output_dir) if self.output_dir else None,
            },
            "gui": {
                "theme": self.gui_theme,
                "show_logs": self.gui_show_logs,
                "remember_settings": self.gui_remember_settings,
            },
        }


# Global default config
_default_config: Optional[Config] = None


def get_config() -> Config:
    """Get the global config instance."""
    global _default_config
    if _default_config is None:
        _default_config = Config.from_env()
    return _default_config


def set_config(config: Config) -> None:
    """Set the global config instance."""
    global _default_config
    _default_config = config
