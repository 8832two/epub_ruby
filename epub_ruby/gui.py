"""
epub-ruby GUI — 极简图形界面
菜单栏：文件(下拉) | 输入输出 | 处理 | API管理 | 帮助（页面切换）
"""

from __future__ import annotations

import json, logging, os, subprocess, sys, traceback
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QAction, QFont, QColor
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGroupBox, QLabel, QLineEdit, QPushButton,
    QSpinBox, QSlider, QTableWidget, QTableWidgetItem,
    QHeaderView, QFileDialog, QMessageBox, QComboBox,
    QProgressBar, QSizePolicy, QAbstractItemView, QStatusBar,
    QDialog, QDialogButtonBox, QFrame, QTextBrowser, QFormLayout,
    QStackedWidget, QTabWidget,
)

LOG_DIR = Path.home() / ".epub_ruby"
LOG_FILE = LOG_DIR / "errors.log"

def _setup_file_logging():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    h = logging.FileHandler(str(LOG_FILE), encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    lg = logging.getLogger("epub_ruby")
    lg.setLevel(logging.DEBUG)
    lg.addHandler(h)

_setup_file_logging()
_logger = logging.getLogger("epub_ruby")

# ═══════════════════════════════════════════════════════════════════════════
# 全局样式
# ═══════════════════════════════════════════════════════════════════════════
#
# 配色:
#   Primary   #2c2c2c   (文字 / 主按钮)
#   Accent    #4a90d9   (焦点 / 强调)
#   Surface   #ffffff   (卡片 / 控件背景)
#   Subtle    #f5f5f5   (次级按钮 / 输入框背景)
#   Border    #e0e0e0   (边框)
#   Muted     #999999   (占位文字)
#   Danger    #c0392b   (错误)

STYLE_SHEET = """
/* ── 全局 ── */
QMainWindow {
    background: #ffffff;
}
QWidget {
    font-size: 12px;
    color: #2c2c2c;
}

/* ── 菜单栏 ── */
QMenuBar {
    background: #ffffff;
    border-bottom: 1px solid #e8e8e8;
    padding: 2px 0;
}
QMenuBar::item {
    padding: 8px 16px;
    margin: 0 2px;
    border-radius: 4px;
}
QMenuBar::item:selected {
    background: #f0f0f0;
}
QMenuBar::item:checked {
    color: #2c2c2c;
    font-weight: 600;
}

QMenu {
    background: #ffffff;
    border: 1px solid #e0e0e0;
    border-radius: 8px;
    padding: 6px;
}
QMenu::item {
    padding: 7px 32px 7px 16px;
    border-radius: 4px;
}
QMenu::item:selected {
    background: #f0f0f0;
}
QMenu::separator {
    height: 1px;
    background: #e8e8e8;
    margin: 4px 10px;
}

/* ── 卡片 (GroupBox) ── */
QGroupBox {
    font-weight: 600;
    border: 1px solid #e8e8e8;
    border-radius: 8px;
    margin-top: 14px;
    padding: 18px 14px 12px 14px;
    background: #ffffff;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 8px;
    color: #555;
}

/* ── 按钮 ── */
QPushButton {
    background: #2c2c2c;
    color: #ffffff;
    border: none;
    border-radius: 6px;
    padding: 7px 18px;
    font-weight: 600;
}
QPushButton:hover {
    background: #444444;
}
QPushButton:pressed {
    background: #1a1a1a;
}
QPushButton:disabled {
    background: #d5d5d5;
    color: #999999;
}

/* 次级按钮 */
QPushButton#secondaryBtn {
    background: #f5f5f5;
    color: #2c2c2c;
    border: 1px solid #e0e0e0;
    font-weight: 500;
}
QPushButton#secondaryBtn:hover {
    background: #e8e8e8;
    border-color: #ccc;
}
QPushButton#secondaryBtn:pressed {
    background: #ddd;
}

/* 执行按钮 */
QPushButton#runBtn {
    background: #ffffff;
    color: #2c2c2c;
    border: 1px solid #d0d0d0;
    border-radius: 6px;
    padding: 8px 28px;
    font-weight: 600;
}
QPushButton#runBtn:hover {
    background: #f5f5f5;
    border-color: #2c2c2c;
}
QPushButton#runBtn:pressed {
    background: #e8e8e8;
}
QPushButton#runBtn:disabled {
    background: #f5f5f5;
    border-color: #e0e0e0;
    color: #bbb;
}

/* 危险按钮 */
QPushButton#dangerBtn {
    background: #c0392b;
    color: #fff;
}
QPushButton#dangerBtn:hover {
    background: #e74c3c;
}

/* ── 输入控件 ── */
QLineEdit, QSpinBox, QComboBox {
    border: 1px solid #e0e0e0;
    border-radius: 6px;
    padding: 6px 10px;
    background: #fafafa;
}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus {
    border-color: #4a90d9;
    background: #ffffff;
}
QComboBox::drop-down {
    border: none;
    width: 24px;
}
QComboBox QAbstractItemView {
    border: 1px solid #e0e0e0;
    border-radius: 4px;
    padding: 4px;
    selection-background-color: #f0f0f0;
    selection-color: #2c2c2c;
}

/* ── 表格 ── */
QTableWidget {
    background: #ffffff;
    border: 1px solid #e8e8e8;
    border-radius: 6px;
    gridline-color: #f0f0f0;
    outline: none;
}
QTableWidget::item {
    padding: 6px 10px;
}
QTableWidget::item:selected {
    background: #f0f0f0;
    color: #2c2c2c;
}
QHeaderView::section {
    background: #fafafa;
    padding: 8px 12px;
    border: none;
    border-bottom: 1px solid #e8e8e8;
    font-weight: 600;
    color: #888;
}

/* ── 滑块 ── */
QSlider::groove:horizontal {
    height: 4px;
    background: #e8e8e8;
    border-radius: 2px;
}
QSlider::handle:horizontal {
    width: 14px;
    height: 14px;
    margin: -5px 0;
    background: #2c2c2c;
    border-radius: 7px;
}
QSlider::handle:horizontal:hover {
    background: #4a90d9;
}
QSlider::sub-page:horizontal {
    background: #bbb;
    border-radius: 2px;
}

/* ── 进度条 ── */
QProgressBar {
    border: none;
    border-radius: 4px;
    text-align: center;
    height: 22px;
    background: #e8e8e8;
    color: #888;
}
QProgressBar::chunk {
    background: #2c2c2c;
    border-radius: 4px;
}

/* ── 拖放区域 ── */
QLabel#dropLabel {
    border: 2px dashed #d5d5d5;
    border-radius: 10px;
    padding: 20px;
    background: #fafafa;
    color: #aaa;
    font-size: 13px;
}
QLabel#dropLabel:hover {
    border-color: #4a90d9;
    background: #f0f5ff;
    color: #555;
}

/* ── 信息标签 ── */
QLabel#hintLabel {
    color: #999;
    font-size: 11px;
}
QLabel#statusLabel {
    color: #888;
    font-size: 11px;
}

/* ── 其他 ── */
QTabWidget::pane {
    border: 1px solid #e8e8e8;
    border-radius: 6px;
    background: #ffffff;
}
QTabBar::tab {
    padding: 8px 24px;
    border: none;
    border-bottom: 2px solid transparent;
    color: #999;
}
QTabBar::tab:selected {
    border-bottom: 2px solid #2c2c2c;
    color: #2c2c2c;
    font-weight: 600;
}
QTabBar::tab:hover {
    color: #555;
}

QRadioButton {
    spacing: 8px;
}

QStatusBar {
    background: #fafafa;
    border-top: 1px solid #e8e8e8;
    color: #888;
}

QScrollArea {
    border: none;
    background: transparent;
}
QTextBrowser {
    border: none;
    background: transparent;
    color: #555;
}
"""

CONFIG_DIR = Path.home() / ".epub_ruby"
CONFIG_FILE = CONFIG_DIR / "config.json"

def load_config():
    if CONFIG_FILE.exists():
        try: return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except: pass
    return {}

def save_config(cfg):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

# ── API 编辑对话框 ──
class APIEditDialog(QDialog):
    PROVIDERS = ["deepseek", "openai", "gemini"]
    MODEL_SUGGESTIONS = {
        "deepseek": ["deepseek-v4-flash", "deepseek-v4-pro", "deepseek-chat"],
        "openai": ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo"],
        "gemini": ["gemini-2.5-flash-preview", "gemini-2.5-pro-preview", "gemini-2.0-flash"],
    }

    def __init__(self, parent=None, edit_data=None):
        super().__init__(parent)
        self.setWindowTitle("编辑 API" if edit_data else "添加 API")
        self.setMinimumWidth(420)
        self._setup_ui()
        if edit_data: self._load_data(edit_data)

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setSpacing(8)
        self._provider_combo = QComboBox()
        self._provider_combo.addItems(self.PROVIDERS)
        self._provider_combo.currentTextChanged.connect(self._on_provider_changed)
        form.addRow("提供商:", self._provider_combo)
        self._api_key_input = QLineEdit()
        self._api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._api_key_input.setPlaceholderText("留空则使用环境变量")
        form.addRow("API Key:", self._api_key_input)
        self._model_combo = QComboBox()
        self._model_combo.setEditable(True)
        self._model_combo.setMinimumWidth(240)
        self._on_provider_changed(self.PROVIDERS[0])
        form.addRow("模型:", self._model_combo)
        self._base_url_input = QLineEdit()
        self._base_url_input.setPlaceholderText("可选，留空使用默认地址")
        form.addRow("Base URL:", self._base_url_input)
        layout.addLayout(form)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def _on_provider_changed(self, provider):
        self._model_combo.clear()
        self._model_combo.addItems(self.MODEL_SUGGESTIONS.get(provider, []))
        if self._model_combo.count(): self._model_combo.setCurrentIndex(0)

    def _load_data(self, data):
        idx = self._provider_combo.findText(data.get("provider", "deepseek"))
        if idx >= 0: self._provider_combo.setCurrentIndex(idx)
        self._api_key_input.setText(data.get("api_key", ""))
        self._model_combo.setCurrentText(data.get("model", ""))
        self._base_url_input.setText(data.get("base_url", ""))

    def get_data(self):
        return {"provider": self._provider_combo.currentText(), "api_key": self._api_key_input.text().strip(),
                "model": self._model_combo.currentText().strip(), "base_url": self._base_url_input.text().strip()}

# ── 拖放区域 ──
class DropZoneFrame(QFrame):
    path_changed = Signal(str)

    def __init__(self, placeholder="拖放 EPUB 文件或目录到此处"):
        super().__init__()
        self.setAcceptDrops(True)
        self.setMinimumHeight(64)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 4)
        self._drop_label = QLabel(placeholder)
        self._drop_label.setObjectName("dropLabel")
        self._drop_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._drop_label.setWordWrap(True)
        layout.addWidget(self._drop_label)
        il = QHBoxLayout(); il.setSpacing(6)
        self._path_input = QLineEdit()
        self._path_input.setPlaceholderText("或输入路径...")
        self._path_input.textChanged.connect(lambda t: os.path.exists(t) and self.path_changed.emit(t))
        il.addWidget(self._path_input)
        for lbl, slot in [("浏览文件...", self._browse_file), ("浏览目录...", self._browse_dir)]:
            b = QPushButton(lbl); b.setObjectName("secondaryBtn"); b.clicked.connect(slot); il.addWidget(b)
        layout.addLayout(il)

    def _browse_file(self):
        p, _ = QFileDialog.getOpenFileName(self, "选择 EPUB 文件", "", "EPUB 文件 (*.epub);;所有文件 (*)")
        if p: self._path_input.setText(p); self.path_changed.emit(p)

    def _browse_dir(self):
        p = QFileDialog.getExistingDirectory(self, "选择目录")
        if p: self._path_input.setText(p); self.path_changed.emit(p)

    def dragEnterEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()
            self._drop_label.setStyleSheet("border:2px dashed #333;border-radius:8px;padding:18px;background:#f5f5f5;color:#333;font-size:12px;")

    def dragLeaveEvent(self, ev): self._drop_label.setStyleSheet("")

    def dropEvent(self, ev):
        self._drop_label.setStyleSheet("")
        if ev.mimeData().urls():
            p = ev.mimeData().urls()[0].toLocalFile()
            if p: self._path_input.setText(p); self.path_changed.emit(p)
            ev.acceptProposedAction()

    def text(self): return self._path_input.text().strip()
    def setText(self, t): self._path_input.setText(t)

# ── 后台处理线程 ──
class ProcessWorker(QThread):
    """Worker thread that runs EPUBHV processing off the main thread.

    Signals:
        progress_signal(current, total)  – per-HTML-file progress
        finished_signal(success, message) – completion
        file_done_signal(path)            – single EPUB file completed
        status_signal(text)               – real-time status message
        llm_stats_signal(calls, errors)   – LLM API stats at end
    """
    progress_signal = Signal(int, int)
    finished_signal = Signal(bool, str)
    file_done_signal = Signal(str)
    status_signal = Signal(str)
    llm_stats_signal = Signal(int, int)

    def __init__(self, input_path, output_dir, use_llm, api_configs,
                 batch_size, max_concurrent, model="", api_key="", base_url=""):
        super().__init__()
        self._input_path = input_path
        self._output_dir = output_dir
        self._use_llm = use_llm
        self._api_configs = api_configs
        self._batch_size = batch_size
        self._max_concurrent = max_concurrent
        self._model = model
        self._api_key = api_key
        self._base_url = base_url

    def run(self):
        try:
            from epub_ruby.core import EPUBHV, list_all_epub_in_dir
            from epub_ruby.api_pool import APIPool, APIConfig

            ip = Path(self._input_path)
            dest = Path(self._output_dir)
            dest.mkdir(parents=True, exist_ok=True)
            files = sorted(list_all_epub_in_dir(ip)) if ip.is_dir() else [ip]
            if not files:
                self.finished_signal.emit(False, "未找到 EPUB 文件。")
                return

            self.status_signal.emit(f"共 {len(files)} 个文件")

            kwargs = {
                "use_llm": self._use_llm,
                "llm_batch_size": self._batch_size,
                "llm_max_concurrent": self._max_concurrent,
            }

            if self._use_llm:
                if self._api_configs:
                    configs = []
                    for c in self._api_configs:
                        try:
                            configs.append(APIConfig(
                                provider=c.get("provider", "deepseek"),
                                api_key=c.get("api_key", ""),
                                model=c.get("model", ""),
                                base_url=c.get("base_url", ""),
                            ))
                        except Exception as e:
                            _logger.warning("skip config: %s", e)
                    if configs:
                        kwargs["llm_pool"] = APIPool(configs)
                        providers = ', '.join(
                            f'{c.provider}:{c.model}' for c in configs
                        )
                        self.status_signal.emit(f"LLM: {providers}")
                    else:
                        self.finished_signal.emit(
                            False, "LLM 模式下没有有效的 API 配置。")
                        return
                else:
                    kwargs["llm_model"] = self._model
                    kwargs["llm_api_key"] = self._api_key
                    kwargs["llm_base_url"] = self._base_url
                    self.status_signal.emit(
                        f"LLM: {self._model or '默认模型'}")

            # ── Pre-scan: count total HTML content files across all EPUBs ──
            self.status_signal.emit("正在扫描文件...")
            total_html = 0
            for f in files:
                try:
                    p = EPUBHV(f)
                    p._extract()
                    p._collect_content_files()
                    total_html += len(p._content_files)
                    p._cleanup()
                except Exception as e:
                    _logger.warning("pre-scan %s: %s", f.name, e)
            self.status_signal.emit(f"共 {total_html} 个内容文件待处理")
            self.progress_signal.emit(0, total_html)

            # ── Process files with per-HTML-file progress ──
            processed_html = [0]  # mutable counter shared by closures

            for epub_idx, f in enumerate(files):
                self.status_signal.emit(
                    f"[{epub_idx+1}/{len(files)}] {f.name}")

                # Factory: each EPUB gets a callback that uses the values from core.py
                def make_progress_cb(counter):
                    def cb(current, total):
                        # Use (current, total) as reported — core.py now combines
                        # LLM batch progress + file-write progress into one scale
                        self.progress_signal.emit(current, total)
                    return cb

                kwargs_with_pb = {**kwargs,
                                  "progress_callback": make_progress_cb(processed_html)}

                try:
                    processor = EPUBHV(f, **kwargs_with_pb)
                    r = processor.run(dest=dest)
                    self.status_signal.emit(f"  ✓ {r.name}")
                    self.file_done_signal.emit(str(r))
                    _logger.info("OK: %s -> %s", f.name, r.name)
                except Exception as e:
                    self.status_signal.emit(f"  ✗ {e}")
                    _logger.error(
                        "FAIL %s: %s\n%s", f.name, e, traceback.format_exc())

            self.progress_signal.emit(total_html, total_html)

            # LLM stats summary
            if self._use_llm:
                try:
                    # Read latest error count from log
                    log_content = ""
                    if LOG_FILE.exists():
                        log_content = LOG_FILE.read_text(encoding="utf-8")
                    error_lines = [l for l in log_content.splitlines()
                                   if "FAILED" in l or "exhausted" in l.lower()]
                    if error_lines:
                        self.status_signal.emit(
                            f"⚠ LLM 错误 ({len(error_lines)} 条)，"
                            f"详见 ~/.epub_ruby/errors.log")
                except Exception:
                    pass

            self.status_signal.emit(f"完成 {len(files)} 个文件")
            self.finished_signal.emit(True, f"成功处理 {len(files)} 个文件。")

        except Exception as e:
            self.status_signal.emit(f"错误: {e}")
            _logger.error("FATAL: %s\n%s", e, traceback.format_exc())
            self.finished_signal.emit(False, str(e))

# ── 主窗口 ──
class MainWindow(QMainWindow):
    PAGE_IO, PAGE_PROC, PAGE_API, PAGE_HELP = 0, 1, 2, 3

    def __init__(self):
        super().__init__()
        self.setWindowTitle("EPUB Ruby")
        self.setMinimumSize(720, 560); self.resize(860, 620)
        self._worker = None
        self._config = load_config()
        self._api_configs = self._config.get("api_configs", [])
        self._setup_menu()
        self._setup_ui()
        self._load_config_to_ui()
        self._switch_page(self.PAGE_IO)

    # ── 菜单 ──
    def _setup_menu(self):
        mb = self.menuBar()
        fm = mb.addMenu("文件(&F)")
        fm.addAction(QAction("打开 EPUB 文件...\tCtrl+O", self, triggered=lambda: self._drop_zone._browse_file()))
        fm.addAction(QAction("打开目录...\tCtrl+D", self, triggered=lambda: self._drop_zone._browse_dir()))
        fm.addSeparator()
        fm.addAction(QAction("报错日志\tCtrl+L", self, triggered=self._open_error_log_dir))
        fm.addSeparator()
        fm.addAction(QAction("退出\tCtrl+Q", self, triggered=self.close))

        self._nav_io = mb.addAction("输入输出"); self._nav_io.setCheckable(True)
        self._nav_io.triggered.connect(lambda: self._switch_page(self.PAGE_IO))
        self._nav_proc = mb.addAction("处理"); self._nav_proc.setCheckable(True)
        self._nav_proc.triggered.connect(lambda: self._switch_page(self.PAGE_PROC))
        self._nav_api = mb.addAction("API管理"); self._nav_api.setCheckable(True)
        self._nav_api.triggered.connect(lambda: self._switch_page(self.PAGE_API))
        self._nav_help = mb.addAction("帮助"); self._nav_help.setCheckable(True)
        self._nav_help.triggered.connect(lambda: self._switch_page(self.PAGE_HELP))
        self._nav_actions = [self._nav_io, self._nav_proc, self._nav_api, self._nav_help]

    def _switch_page(self, idx):
        self._stack.setCurrentIndex(idx)
        for i, a in enumerate(self._nav_actions): a.setChecked(i == idx)
        if idx == self.PAGE_PROC: self._update_api_summary()

    # ── UI ──
    def _setup_ui(self):
        c = QWidget(); self.setCentralWidget(c)
        lo = QVBoxLayout(c); lo.setContentsMargins(0, 0, 0, 0); lo.setSpacing(0)
        self._stack = QStackedWidget()
        self._stack.addWidget(self._create_io_page())
        self._stack.addWidget(self._create_proc_page())
        self._stack.addWidget(self._create_api_page())
        self._stack.addWidget(self._create_help_page())
        lo.addWidget(self._stack, 1)
        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._status_label = QLabel("就绪")
        self._status_label.setObjectName("statusLabel")
        self._status_bar.addWidget(self._status_label)

    # ── 页面0: 输入输出 ──
    def _create_io_page(self):
        p = QWidget(); l = QVBoxLayout(p); l.setContentsMargins(20, 16, 20, 16); l.setSpacing(10)

        card = QGroupBox("输入")
        cl = QVBoxLayout(card); cl.setSpacing(8)
        self._drop_zone = DropZoneFrame("拖放 EPUB 文件或目录到此处\n支持 .epub 文件及包含 epub 的文件夹")
        self._drop_zone.path_changed.connect(self._on_input_changed)
        cl.addWidget(self._drop_zone)
        l.addWidget(card)

        out_card = QGroupBox("输出")
        oc = QVBoxLayout(out_card); oc.setSpacing(6)
        orow = QHBoxLayout(); orow.setSpacing(6)
        orow.addWidget(QLabel("输出目录:"))
        self._output_input = QLineEdit(); self._output_input.setPlaceholderText("默认 ./output")
        orow.addWidget(self._output_input, 1)
        ob = QPushButton("浏览..."); ob.setObjectName("secondaryBtn"); ob.clicked.connect(self._browse_output)
        orow.addWidget(ob); oc.addLayout(orow)
        l.addWidget(out_card)
        l.addStretch()
        return p

    # ── 页面1: 处理（含普通/LLM子页） ──
    def _create_proc_page(self):
        p = QWidget(); l = QVBoxLayout(p); l.setContentsMargins(20, 16, 20, 16); l.setSpacing(10)
        self._proc_tabs = QTabWidget()
        self._proc_tabs.addTab(self._create_normal_subpage(), "普通模式")
        self._proc_tabs.addTab(self._create_llm_subpage(), "LLM 模式")
        self._proc_tabs.currentChanged.connect(self._on_proc_tab_changed)
        l.addWidget(self._proc_tabs, 1)
        return p

    def _create_normal_subpage(self):
        p = QWidget(); l = QVBoxLayout(p)
        l.setContentsMargins(20, 16, 20, 16); l.setSpacing(12)
        info = QLabel(
            "使用 fugashi (UniDic) 词典注音，速度快，完全离线。\n"
            "不需要任何 API Key，适合快速批量处理。")
        info.setObjectName("hintLabel"); info.setWordWrap(True)
        l.addWidget(info)

        self._progress_bar = QProgressBar()
        self._progress_bar.setVisible(False)
        self._progress_bar.setFixedHeight(22)
        l.addWidget(self._progress_bar)

        self._status_info = QLabel("")
        self._status_info.setObjectName("hintLabel")
        self._status_info.setWordWrap(True)
        l.addWidget(self._status_info)

        l.addStretch()
        btn_row = QHBoxLayout(); btn_row.addStretch()
        self._run_btn_normal = QPushButton("▶  开始处理")
        self._run_btn_normal.setObjectName("runBtn")
        self._run_btn_normal.clicked.connect(lambda: self._start_processing())
        btn_row.addWidget(self._run_btn_normal)
        l.addLayout(btn_row)
        l.addSpacing(8)
        return p

    def _create_llm_subpage(self):
        p = QWidget(); l = QVBoxLayout(p); l.setContentsMargins(20, 16, 20, 16); l.setSpacing(8)

        # API 池状态
        sr = QHBoxLayout()
        self._api_summary_label = QLabel("未配置 API 池")
        self._api_summary_label.setObjectName("hintLabel")
        sr.addWidget(self._api_summary_label)
        sr.addStretch()
        gb = QPushButton("API 管理 →")
        gb.setObjectName("secondaryBtn")
        gb.clicked.connect(lambda: self._switch_page(self.PAGE_API))
        sr.addWidget(gb)
        l.addLayout(sr)

        # 参数
        param_card = QGroupBox("调用参数")
        pc = QVBoxLayout(param_card); pc.setSpacing(8)
        br = QHBoxLayout(); br.setSpacing(8)
        br.addWidget(QLabel("批次:"))
        self._batch_slider = QSlider(Qt.Orientation.Horizontal)
        self._batch_slider.setRange(5, 200); self._batch_slider.setValue(60)
        self._batch_slider.setTickInterval(25); self._batch_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        br.addWidget(self._batch_slider, 1)
        self._batch_spin = QSpinBox(); self._batch_spin.setRange(5, 200); self._batch_spin.setValue(60)
        self._batch_spin.setSuffix(" 句/次"); self._batch_spin.setFixedWidth(90)
        self._batch_slider.valueChanged.connect(self._batch_spin.setValue)
        self._batch_spin.valueChanged.connect(self._batch_slider.setValue)
        br.addWidget(self._batch_spin); pc.addLayout(br)

        cr = QHBoxLayout(); cr.setSpacing(8)
        cr.addWidget(QLabel("并发:"))
        self._concurrent_slider = QSlider(Qt.Orientation.Horizontal)
        self._concurrent_slider.setRange(0, 50); self._concurrent_slider.setValue(10)
        self._concurrent_slider.setTickInterval(5); self._concurrent_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        cr.addWidget(self._concurrent_slider, 1)
        self._concurrent_spin = QSpinBox(); self._concurrent_spin.setRange(0, 50); self._concurrent_spin.setValue(10)
        self._concurrent_spin.setSpecialValueText("不限"); self._concurrent_spin.setFixedWidth(70)
        self._concurrent_slider.valueChanged.connect(self._concurrent_spin.setValue)
        self._concurrent_spin.valueChanged.connect(self._concurrent_slider.setValue)
        cr.addWidget(self._concurrent_spin)
        hint_lbl = QLabel("Gemini 免费层建议 5–10；0 = 不限")
        hint_lbl.setObjectName("hintLabel")
        cr.addWidget(hint_lbl)
        cr.addStretch(); pc.addLayout(cr)
        l.addWidget(param_card)

        # 单 API 快捷配置（无 API 池时使用）
        fb_card = QGroupBox("单 API 快捷配置")
        fc = QVBoxLayout(fb_card)
        hint2 = QLabel("不配置 API 池时，使用此处的单模型。优先使用 API 池。")
        hint2.setObjectName("hintLabel"); hint2.setWordWrap(True)
        fc.addWidget(hint2)
        fr = QHBoxLayout(); fr.setSpacing(6)
        fr.addWidget(QLabel("模型:"))
        self._default_model = QLineEdit()
        self._default_model.setPlaceholderText("deepseek-v4-flash")
        self._default_model.setMaximumWidth(200)
        fr.addWidget(self._default_model)
        fr.addWidget(QLabel("Key:"))
        self._default_api_key = QLineEdit()
        self._default_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._default_api_key.setPlaceholderText("留空=环境变量")
        self._default_api_key.setMaximumWidth(200)
        fr.addWidget(self._default_api_key)
        fr.addStretch()
        fc.addLayout(fr)
        l.addWidget(fb_card)

        # 进度条
        self._progress_bar_llm = QProgressBar()
        self._progress_bar_llm.setVisible(False)
        self._progress_bar_llm.setFixedHeight(22)
        l.addWidget(self._progress_bar_llm)

        self._status_info_llm = QLabel("")
        self._status_info_llm.setObjectName("hintLabel")
        self._status_info_llm.setWordWrap(True)
        l.addWidget(self._status_info_llm)

        l.addStretch()
        btn_row = QHBoxLayout(); btn_row.addStretch()
        self._run_btn_llm = QPushButton("▶  开始处理")
        self._run_btn_llm.setObjectName("runBtn")
        self._run_btn_llm.clicked.connect(lambda: self._start_processing())
        btn_row.addWidget(self._run_btn_llm)
        l.addLayout(btn_row)
        l.addSpacing(8)
        return p

    # ── 页面2: API管理 ──
    def _create_api_page(self):
        p = QWidget(); l = QVBoxLayout(p)
        l.setContentsMargins(20, 16, 20, 16); l.setSpacing(8)
        hint = QLabel("按优先级排列，额度用尽自动切换下一个。")
        hint.setObjectName("hintLabel"); hint.setWordWrap(True)
        l.addWidget(hint)

        tr = QHBoxLayout(); tr.setSpacing(8)
        self._api_table = QTableWidget(0, 4)
        self._api_table.setHorizontalHeaderLabels(["提供商", "API Key", "模型", "Base URL"])
        for i, m in enumerate([QHeaderView.ResizeMode.ResizeToContents, QHeaderView.ResizeMode.Stretch, QHeaderView.ResizeMode.Stretch, QHeaderView.ResizeMode.Stretch]):
            self._api_table.horizontalHeader().setSectionResizeMode(i, m)
        self._api_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._api_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._api_table.verticalHeader().setVisible(False)
        tr.addWidget(self._api_table, 1)

        bp = QWidget(); bp.setFixedWidth(72); bl = QVBoxLayout(bp); bl.setSpacing(4); bl.setContentsMargins(0, 0, 0, 0)
        for lbl, slot in [("＋", self._add_api), ("－", self._remove_api), ("✎", self._edit_api), ("▲", self._move_api_up), ("▼", self._move_api_down)]:
            b = QPushButton(lbl); b.setObjectName("secondaryBtn"); b.clicked.connect(slot); bl.addWidget(b)
        bl.addStretch(); tr.addWidget(bp); l.addLayout(tr, 1)
        return p

    def _create_help_page(self):
        p = QWidget(); l = QVBoxLayout(p)
        l.setContentsMargins(20, 16, 20, 16)
        b = QTextBrowser(); b.setOpenExternalLinks(True)
        b.setHtml("""
<h3>EPUB Ruby — 振假名标注工具</h3>
<p>为 EPUB 日文书籍自动添加振假名（ruby / furigana），让阅读更轻松。</p>

<p><b>基本用法</b></p>
<ol>
<li><b>输入输出</b> — 拖放或浏览选择 EPUB 文件（或包含 epub 的文件夹），设置输出目录。</li>
<li><b>处理</b> — 选择「普通模式」或「LLM 模式」标签页，点击 <b>▶ 开始处理</b>。</li>
<li><b>API 管理</b> — 仅 LLM 模式需要。添加一个或多个 API（支持 DeepSeek / OpenAI / Gemini），按优先级排列，额度用尽自动切换下一个。</li>
</ol>

<p><b>两种模式的区别</b></p>
<ul>
<li><b>普通模式</b> — 使用词典自动注音，完全离线、速度快，适合快速批量处理，无需任何配置。</li>
<li><b>LLM 模式</b> — 由大语言模型根据上下文智能判断读音，标注更准确，但需要配置 API Key 且有网络请求。</li>
</ul>

<p><b>小提示</b></p>
<ul>
<li>LLM 模式下可在「处理」页调整批次大小和并发数。</li>
<li>处理出错时，可通过菜单栏 <b>文件 → 报错日志</b> 查看详细错误信息。</li>
<li>LLM 调用失败会自动重试 3 次，仍失败则跳过该批次。</li>
</ul>

<p style="margin-top:24px;"><a href="https://github.com/8832two/epub_ruby">GitHub</a></p>
<p style="color:#999;font-size:11px;">
普通模式提供: 分词 fugashi / UniDic &nbsp;·&nbsp; EPUB 处理 epubhv
<br>LLM 模式提供: @8832xb
</p>
""")
        l.addWidget(b, 1)
        return p

    # ── 事件 ──
    def _on_input_changed(self, path):
        label = os.path.basename(path) if os.path.isfile(path) else path
        self._status_label.setText(f"已选择: {label}")
        self._status_label.setStyleSheet("color:#2c2c2c;font-size:11px;")

    def _on_proc_tab_changed(self, idx):
        pass

    def _browse_output(self):
        p = QFileDialog.getExistingDirectory(self, "选择输出目录")
        if p: self._output_input.setText(p)

    # ── API 表格 ──
    def _add_api(self):
        dlg = APIEditDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            row = self._api_table.rowCount(); self._api_table.insertRow(row)
            self._set_api_row(row, dlg.get_data()); self._sync(); self._update_api_summary()

    def _edit_api(self):
        row = self._api_table.currentRow()
        if row < 0: QMessageBox.warning(self, "提示", "请先选择要编辑的 API。"); return
        dlg = APIEditDialog(self, edit_data=self._get_api_row(row))
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._set_api_row(row, dlg.get_data()); self._sync(); self._update_api_summary()

    def _remove_api(self):
        row = self._api_table.currentRow()
        if row < 0: QMessageBox.warning(self, "提示", "请先选择要删除的 API。"); return
        name = self._api_table.item(row, 0).text() if self._api_table.item(row, 0) else ""
        if QMessageBox.question(self, "确认", f"确定删除 {name}?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            self._api_table.removeRow(row); self._sync(); self._update_api_summary()

    def _move_api_up(self):
        row = self._api_table.currentRow()
        if row > 0: self._swap_rows(row, row - 1); self._api_table.selectRow(row - 1); self._sync()

    def _move_api_down(self):
        row = self._api_table.currentRow()
        if 0 <= row < self._api_table.rowCount() - 1: self._swap_rows(row, row + 1); self._api_table.selectRow(row + 1); self._sync()

    def _swap_rows(self, r1, r2):
        d1, d2 = self._get_api_row(r1), self._get_api_row(r2)
        self._set_api_row(r1, d2); self._set_api_row(r2, d1)

    def _get_api_row(self, row):
        its = [self._api_table.item(row, c) for c in range(4)]
        return {"provider": its[0].text() if its[0] else "", "api_key": its[1].text() if its[1] else "",
                "model": its[2].text() if its[2] else "", "base_url": its[3].text() if its[3] else ""}

    def _set_api_row(self, row, data):
        key = data.get("api_key", "")
        dk = key[:8] + "..." if len(key) > 8 else key
        for col, val in enumerate([data.get("provider",""), dk, data.get("model",""), data.get("base_url","")]):
            item = QTableWidgetItem(val)
            if col == 1 and key: item.setToolTip(key); item.setData(Qt.ItemDataRole.UserRole, key)
            self._api_table.setItem(row, col, item)
        self._api_table.resizeRowsToContents()

    def _sync(self):
        self._api_configs = self._get_api_configs(); self._save_config_from_ui()

    def _get_api_configs(self):
        configs = []
        for row in range(self._api_table.rowCount()):
            ki = self._api_table.item(row, 1)
            key = (ki.data(Qt.ItemDataRole.UserRole) or ki.text()) if ki else ""
            configs.append({"provider": self._api_table.item(row,0).text() if self._api_table.item(row,0) else "",
                            "api_key": key,
                            "model": self._api_table.item(row,2).text() if self._api_table.item(row,2) else "",
                            "base_url": self._api_table.item(row,3).text() if self._api_table.item(row,3) else ""})
        return configs

    def _update_api_summary(self):
        if not hasattr(self, '_api_summary_label'):
            return
        if self._api_configs:
            ps = [f"{c['provider']}:{c['model']}" for c in self._api_configs]
            self._api_summary_label.setText(f"API 池: {', '.join(ps)}")
            self._api_summary_label.setStyleSheet(
                "color:#2c2c2c;font-size:11px;font-weight:600;")
        else:
            self._api_summary_label.setText("未配置 API 池（将使用下方单 API 配置）")
            self._api_summary_label.setStyleSheet(
                "color:#999;font-size:11px;")

    # ── 处理流程 ──
    def _start_processing(self):
        ip = self._drop_zone.text()
        if not ip or not os.path.exists(ip):
            QMessageBox.warning(self, "输入错误",
                                "请先在「输入输出」页选择文件或目录。")
            self._switch_page(self.PAGE_IO)
            return

        od = self._output_input.text().strip() or "./output"
        use_llm = self._proc_tabs.currentIndex() == 1

        if use_llm and not self._api_configs:
            if not self._default_api_key.text().strip():
                reply = QMessageBox.question(
                    self, "LLM 模式",
                    "未配置 API 池，也未设置默认 API Key。\n"
                    "LLM 模式需要至少一个 API 才能工作。\n\n"
                    "是否切换到普通模式？",
                    QMessageBox.StandardButton.Yes |
                    QMessageBox.StandardButton.No)
                if reply == QMessageBox.StandardButton.Yes:
                    self._proc_tabs.setCurrentIndex(0)
                    use_llm = False
                else:
                    return

        self._save_config_from_ui()
        self._set_controls_enabled(False)

        is_llm = use_llm
        pb = self._progress_bar_llm if is_llm else self._progress_bar
        si = self._status_info_llm if is_llm else self._status_info
        pb.setVisible(True); pb.setValue(0)
        si.setText("处理中…")
        si.setStyleSheet("color:#2c2c2c;font-size:12px;font-weight:600;")
        self._status_label.setText("处理中…")
        self._status_label.setStyleSheet("color:#2c2c2c;font-size:11px;")

        self._worker = ProcessWorker(
            input_path=ip, output_dir=od, use_llm=use_llm,
            api_configs=self._api_configs if use_llm else [],
            batch_size=self._batch_spin.value(),
            max_concurrent=self._concurrent_spin.value(),
            model=self._default_model.text().strip(),
            api_key=self._default_api_key.text().strip(),
            base_url="",
        )
        self._worker.status_signal.connect(lambda t: si.setText(t))
        self._worker.progress_signal.connect(
            lambda c, t: (pb.setMaximum(t), pb.setValue(c)))
        self._worker.finished_signal.connect(self._on_finished)
        self._worker.file_done_signal.connect(
            lambda f: self._status_label.setText(
                f"已完成: {os.path.basename(f)}"))
        self._worker.start()

    def _on_finished(self, success, msg):
        self._set_controls_enabled(True)
        is_llm = self._proc_tabs.currentIndex() == 1
        pb = self._progress_bar_llm if is_llm else self._progress_bar
        si = self._status_info_llm if is_llm else self._status_info
        pb.setVisible(False)

        if success:
            si.setText("✓ 完成")
            si.setStyleSheet("color:#27ae60;font-size:12px;font-weight:600;")
            self._status_label.setText(msg)
            self._status_label.setStyleSheet("color:#2c2c2c;font-size:11px;")
        else:
            si.setText(f"✗ 失败: {msg}")
            si.setStyleSheet("color:#c0392b;font-size:12px;font-weight:600;")
            self._status_label.setText(f"错误: {msg}")
            self._status_label.setStyleSheet("color:#c0392b;font-size:11px;")
            QMessageBox.critical(self, "处理失败", msg)

    def _set_controls_enabled(self, enabled):
        for name, w in [("_drop_zone", self._drop_zone), ("_output_input", self._output_input),
                         ("_batch_slider", self._batch_slider), ("_batch_spin", self._batch_spin),
                         ("_concurrent_slider", self._concurrent_slider), ("_concurrent_spin", self._concurrent_spin),
                         ("_default_model", self._default_model), ("_default_api_key", self._default_api_key),
                         ("_run_btn_normal", self._run_btn_normal), ("_run_btn_llm", self._run_btn_llm)]:
            try: w.setEnabled(enabled)
            except Exception as e: _logger.warning("setEnabled(%s): %s", name, e)

    # ── 报错日志 ──
    def _open_error_log_dir(self):
        ld = str(LOG_DIR)
        try:
            if sys.platform == "win32": os.startfile(ld)
            elif sys.platform == "darwin": subprocess.Popen(["open", ld])
            else: subprocess.Popen(["xdg-open", ld])
        except Exception as e:
            QMessageBox.warning(self, "提示", f"无法打开: {ld}\n{e}\n\n日志: {LOG_FILE}")

    # ── 配置 ──
    def _save_config_from_ui(self):
        try:
            self._config.update({"input_path": self._drop_zone.text(), "output_dir": self._output_input.text(),
                                 "use_llm": self._proc_tabs.currentIndex() == 1, "api_configs": self._api_configs,
                                 "batch_size": self._batch_spin.value(), "max_concurrent": self._concurrent_spin.value(),
                                 "default_model": self._default_model.text(), "default_api_key": self._default_api_key.text()})
            save_config(self._config)
        except Exception as e: _logger.warning("save config: %s", e)

    def _load_config_to_ui(self):
        cfg = self._config
        if cfg.get("input_path"): self._drop_zone.setText(cfg["input_path"])
        if cfg.get("output_dir"): self._output_input.setText(cfg["output_dir"])
        if cfg.get("use_llm"): self._proc_tabs.setCurrentIndex(1)
        if v := cfg.get("batch_size"): self._batch_spin.setValue(v)
        if (v := cfg.get("max_concurrent", 0)) is not None: self._concurrent_spin.setValue(v)
        if cfg.get("default_model"): self._default_model.setText(cfg["default_model"])
        if cfg.get("default_api_key"): self._default_api_key.setText(cfg["default_api_key"])
        self._api_configs = cfg.get("api_configs", [])
        for data in self._api_configs:
            row = self._api_table.rowCount(); self._api_table.insertRow(row); self._set_api_row(row, data)
        self._update_api_summary()

    def closeEvent(self, ev):
        try: self._save_config_from_ui()
        except: pass
        if self._worker and self._worker.isRunning():
            if QMessageBox.question(self, "确认退出", "任务运行中，确定退出?",
                                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) == QMessageBox.StandardButton.No:
                ev.ignore(); return
            self._worker.terminate(); self._worker.wait(2000)
        ev.accept()

def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE_SHEET)
    app.setFont(QFont("Segoe UI", 10))
    w = MainWindow(); w.show(); sys.exit(app.exec())

if __name__ == "__main__":
    main()
