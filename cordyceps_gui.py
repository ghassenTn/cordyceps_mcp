#!/usr/bin/env python3
"""
Cordyceps Search — EngramDB Query Studio (PyQt6)

Interactive GUI application to run Query DSL against EngramDB engine.
"""

import os
import sys
import time
import yaml
from concurrent.futures import ThreadPoolExecutor

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QSize
from PyQt6.QtGui import (
    QFont, QColor, QPalette, QAction, QIcon, QGuiApplication, QKeySequence, QShortcut
)
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QLineEdit, QPushButton, QComboBox, QPlainTextEdit, QTextEdit,
    QTabWidget, QTreeWidget, QTreeWidgetItem, QHeaderView, QCheckBox,
    QFileDialog, QMessageBox, QStatusBar, QFrame, QSizePolicy, QSplitter,
    QAbstractItemView
)

# Ensure current directory is in python path
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from src.database import GraphDB
from src.watcher.sync_handler import GraphSyncHandler
from src.query import query as run_dsl_query


PRESET_QUERIES = [
    ("High Impact Functions (Blast Radius >= 3)",
     "GET functions WHERE blast_radius_score >= 3 LIMIT 20"),
    ("Exclude Auth Views (Not Regex !=~)",
     "GET functions WHERE name !=~ '^(auth_|login_)' AND is_exported == true LIMIT 20"),
    ("Async Functions with Multiple Parameters",
     "GET functions WHERE is_async == true AND param_count >= 2"),
    ("Unused Public Exported Functions (Dead Code)",
     "GET functions WHERE is_exported == true AND callers_count == 0"),
    ("Long Parameter Lists (Code Smell)",
     "GET functions WHERE param_count > 4 AND lines_count > 30"),
    ("Streaming Generator Functions",
     "GET functions WHERE is_generator == true"),
    ("Classes with Active Callers",
     "GET classes WHERE callers_count > 0"),
    ("Search Functions by Keyword 'sales'",
     "GET functions WHERE name CONTAINS 'sales' LIMIT 20"),
    ("Functions with Body Containing 'apiFetch'",
     "GET functions WHERE body CONTAINS 'apiFetch' LIMIT 5"),
    ("All Files",
     "GET files LIMIT 20"),
]

EXCLUDED_DIRS = {
    'node_modules', 'venv', 'env', '.venv', '.env',
    '__pycache__', 'target', 'dist', 'build', 'out',
    'migrations', 'alembic', '.git', '.idea', '.vscode'
}

# ─────────────────────────────────────────────────────────────────────────────
# 3-Color Professional Palette
# ─────────────────────────────────────────────────────────────────────────────
COLOR_BG       = "#0F172A"   # deep navy     — main background
COLOR_SURFACE  = "#1E293B"   # slate         — elevated cards, inputs, tables
COLOR_ACCENT   = "#38BDF8"   # sky blue      — primary action / focus

# Functional neutrals (not counted as "main" colors — required for legibility)
COLOR_TEXT        = "#E2E8F0"
COLOR_TEXT_MUTED  = "#94A3B8"
COLOR_BORDER      = "#334155"
COLOR_HOVER       = "#334155"
COLOR_PRESSED     = "#0EA5E9"

# Font stack
FONT_FAMILY = "Inter, Segoe UI, SF Pro Display, system-ui, sans-serif"
FONT_MONO   = "JetBrains Mono, Consolas, SF Mono, monospace"


# ─────────────────────────────────────────────────────────────────────────────
# Stylesheet
# ─────────────────────────────────────────────────────────────────────────────
STYLESHEET = f"""
QWidget {{
    background-color: {COLOR_BG};
    color: {COLOR_TEXT};
    font-family: "{FONT_FAMILY}";
    font-size: 13px;
}}

/* ── Top-level frames & splitters ─────────────────────────── */
QMainWindow, QDialog {{ background-color: {COLOR_BG}; }}
QSplitter::handle {{ background-color: {COLOR_BORDER}; }}
QSplitter::handle:horizontal {{ width: 1px; }}
QSplitter::handle:vertical   {{ height: 1px; }}

/* ── Labels ──────────────────────────────────────────────── */
QLabel {{ background: transparent; color: {COLOR_TEXT}; }}
QLabel[role="title"]    {{ font-size: 18px; font-weight: 600; color: {COLOR_TEXT}; }}
QLabel[role="subtitle"] {{ font-size: 12px; color: {COLOR_TEXT_MUTED}; letter-spacing: 0.5px; }}
QLabel[role="stat"]     {{ color: {COLOR_TEXT_MUTED}; font-size: 11px; letter-spacing: 0.5px; text-transform: uppercase; }}
QLabel[role="statValue"] {{ color: {COLOR_ACCENT}; font-size: 22px; font-weight: 600; }}

/* ── Cards / Containers ──────────────────────────────────── */
QFrame[role="card"] {{
    background-color: {COLOR_SURFACE};
    border: 1px solid {COLOR_BORDER};
    border-radius: 10px;
}}
QFrame[role="divider"] {{
    background-color: {COLOR_BORDER};
    max-height: 1px;
    min-height: 1px;
    border: none;
}}

/* ── Input fields ────────────────────────────────────────── */
QLineEdit, QPlainTextEdit, QTextEdit {{
    background-color: {COLOR_SURFACE};
    color: {COLOR_TEXT};
    border: 1px solid {COLOR_BORDER};
    border-radius: 6px;
    padding: 8px 10px;
    selection-background-color: {COLOR_ACCENT};
    selection-color: {COLOR_BG};
}}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus {{
    border: 1px solid {COLOR_ACCENT};
}}
QPlainTextEdit, QTextEdit {{
    font-family: "{FONT_MONO}";
    font-size: 12px;
}}

/* ── ComboBox ────────────────────────────────────────────── */
QComboBox {{
    background-color: {COLOR_SURFACE};
    color: {COLOR_TEXT};
    border: 1px solid {COLOR_BORDER};
    border-radius: 6px;
    padding: 7px 30px 7px 10px;
    min-height: 20px;
}}
QComboBox:hover  {{ border: 1px solid {COLOR_ACCENT}; }}
QComboBox:focus  {{ border: 1px solid {COLOR_ACCENT}; }}
QComboBox::drop-down {{
    subcontrol-origin: padding;
    subcontrol-position: top right;
    width: 24px;
    border: none;
}}
QComboBox QAbstractItemView {{
    background-color: {COLOR_SURFACE};
    color: {COLOR_TEXT};
    border: 1px solid {COLOR_BORDER};
    selection-background-color: {COLOR_ACCENT};
    selection-color: {COLOR_BG};
    outline: 0;
    padding: 4px;
}}

/* ── Buttons ─────────────────────────────────────────────── */
QPushButton {{
    background-color: {COLOR_SURFACE};
    color: {COLOR_TEXT};
    border: 1px solid {COLOR_BORDER};
    border-radius: 6px;
    padding: 8px 16px;
    font-weight: 500;
}}
QPushButton:hover {{
    background-color: {COLOR_HOVER};
    border: 1px solid {COLOR_ACCENT};
}}
QPushButton:pressed {{
    background-color: {COLOR_PRESSED};
    color: {COLOR_BG};
    border: 1px solid {COLOR_PRESSED};
}}
QPushButton:disabled {{
    color: {COLOR_TEXT_MUTED};
    border: 1px solid {COLOR_BORDER};
}}
QPushButton[role="primary"] {{
    background-color: {COLOR_ACCENT};
    color: {COLOR_BG};
    border: 1px solid {COLOR_ACCENT};
    font-weight: 600;
}}
QPushButton[role="primary"]:hover {{
    background-color: {COLOR_PRESSED};
    border: 1px solid {COLOR_PRESSED};
}}

/* ── CheckBox ────────────────────────────────────────────── */
QCheckBox {{
    spacing: 8px;
    color: {COLOR_TEXT};
    background: transparent;
}}
QCheckBox::indicator {{
    width: 16px;
    height: 16px;
    border: 1px solid {COLOR_BORDER};
    border-radius: 3px;
    background: {COLOR_SURFACE};
}}
QCheckBox::indicator:hover  {{ border: 1px solid {COLOR_ACCENT}; }}
QCheckBox::indicator:checked {{
    background: {COLOR_ACCENT};
    border: 1px solid {COLOR_ACCENT};
    image: none;
}}

/* ── Tabs ────────────────────────────────────────────────── */
QTabWidget::pane {{
    border: 1px solid {COLOR_BORDER};
    border-radius: 8px;
    background: {COLOR_SURFACE};
    top: -1px;
}}
QTabBar {{ background: transparent; }}
QTabBar::tab {{
    background: transparent;
    color: {COLOR_TEXT_MUTED};
    padding: 9px 18px;
    margin-right: 2px;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    border: 1px solid transparent;
}}
QTabBar::tab:hover {{ color: {COLOR_TEXT}; }}
QTabBar::tab:selected {{
    color: {COLOR_ACCENT};
    background: {COLOR_SURFACE};
    border: 1px solid {COLOR_BORDER};
    border-bottom: 1px solid {COLOR_SURFACE};
}}

/* ── Tree (Table) ────────────────────────────────────────── */
QTreeWidget, QTreeView {{
    background-color: {COLOR_SURFACE};
    color: {COLOR_TEXT};
    border: 1px solid {COLOR_BORDER};
    border-radius: 6px;
    gridline-color: {COLOR_BORDER};
    outline: 0;
    selection-background-color: {COLOR_ACCENT};
    selection-color: {COLOR_BG};
    alternate-background-color: {COLOR_BG};
}}
QHeaderView::section {{
    background-color: {COLOR_BG};
    color: {COLOR_TEXT_MUTED};
    padding: 8px 10px;
    border: none;
    border-right: 1px solid {COLOR_BORDER};
    border-bottom: 1px solid {COLOR_BORDER};
    font-weight: 600;
    font-size: 11px;
    letter-spacing: 0.5px;
    text-transform: uppercase;
}}
QHeaderView::section:hover {{ color: {COLOR_ACCENT}; }}
QTreeWidget::item, QTreeView::item {{
    padding: 4px 2px;
    border: none;
}}
QTreeWidget::item:selected, QTreeView::item:selected {{
    background-color: {COLOR_ACCENT};
    color: {COLOR_BG};
}}

/* ── Scrollbars ──────────────────────────────────────────── */
QScrollBar:vertical {{
    background: {COLOR_BG};
    width: 10px;
    border: none;
}}
QScrollBar::handle:vertical {{
    background: {COLOR_BORDER};
    border-radius: 4px;
    min-height: 24px;
    margin: 2px;
}}
QScrollBar::handle:vertical:hover {{ background: {COLOR_ACCENT}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0; background: none;
}}
QScrollBar:horizontal {{
    background: {COLOR_BG};
    height: 10px;
    border: none;
}}
QScrollBar::handle:horizontal {{
    background: {COLOR_BORDER};
    border-radius: 4px;
    min-width: 24px;
    margin: 2px;
}}
QScrollBar::handle:horizontal:hover {{ background: {COLOR_ACCENT}; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0; background: none;
}}

/* ── Status bar ──────────────────────────────────────────── */
QStatusBar {{
    background: {COLOR_SURFACE};
    color: {COLOR_TEXT_MUTED};
    border-top: 1px solid {COLOR_BORDER};
    padding: 4px 10px;
}}
QStatusBar::item {{ border: none; }}
"""


# ─────────────────────────────────────────────────────────────────────────────
# Background workers
# ─────────────────────────────────────────────────────────────────────────────
class ScanWorker(QThread):
    """Scans the workspace and builds the EngramDB index off the UI thread."""
    finished_ok  = pyqtSignal(float, dict)   # elapsed_ms, stats
    failed       = pyqtSignal(str)           # error message

    def __init__(self, workspace_path: str):
        super().__init__()
        self.workspace_path = workspace_path

    def run(self):
        try:
            start_time = time.time()

            from src.database import get_graph_db
            db_instance = get_graph_db(self.workspace_path)

            event_handler = GraphSyncHandler(self.workspace_path)
            source_files = []
            for root, dirs, files in os.walk(self.workspace_path):
                dirs[:] = [d for d in dirs if not d.startswith('.') and d not in EXCLUDED_DIRS]
                for file in files:
                    if file.endswith(event_handler.supported_extensions):
                        source_files.append(os.path.join(root, file))

            if source_files:
                def _parse(fp):
                    try:
                        return event_handler.parser.parse_file(fp)
                    except Exception:
                        return None

                with ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as executor:
                    parsed_list = list(executor.map(_parse, source_files))

                for parsed in parsed_list:
                    if parsed:
                        event_handler.update_file_in_graph(
                            parsed["file_path"], skip_rebuild=True, pre_parsed_data=parsed
                        )

            db_instance.client.build()

            elapsed = (time.time() - start_time) * 1000
            stats = db_instance.get_network_stats()
            self.finished_ok.emit(elapsed, stats)

        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


class QueryWorker(QThread):
    """Runs the user's DSL query off the UI thread."""
    finished_ok = pyqtSignal(dict, float)    # result, elapsed_ms
    failed      = pyqtSignal(str)

    def __init__(self, db_client, query_str: str, expand_body: bool):
        super().__init__()
        self.db_client = db_client
        self.query_str = query_str
        self.expand_body = expand_body

    def run(self):
        try:
            start_time = time.time()
            res = run_dsl_query(self.db_client, self.query_str, expand_body=self.expand_body)
            elapsed = (time.time() - start_time) * 1000
            self.finished_ok.emit(res, elapsed)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


# ─────────────────────────────────────────────────────────────────────────────
# Reusable helpers
# ─────────────────────────────────────────────────────────────────────────────
def make_card_layout(card: QFrame) -> QVBoxLayout:
    """Returns a vertical layout with consistent internal padding for a card."""
    lay = QVBoxLayout(card)
    lay.setContentsMargins(16, 14, 16, 14)
    lay.setSpacing(10)
    return lay


# ─────────────────────────────────────────────────────────────────────────────
# Main Window
# ─────────────────────────────────────────────────────────────────────────────
class CordycepsQueryStudio(QMainWindow):
    def __init__(self):
        super().__init__()

        self.setWindowTitle("Cordyceps Search — EngramDB Query Studio")
        self.resize(1240, 820)
        self.setMinimumSize(960, 640)

        self.db = None
        self.workspace_path = os.environ.get("WORKSPACE_PATH", os.getcwd())
        self.current_results: dict | None = None
        self._scan_worker: ScanWorker | None = None
        self._query_worker: QueryWorker | None = None

        self._build_ui()
        self._build_statusbar()

        # Auto-load workspace shortly after start
        QApplication.instance().processEvents()
        self._start_scan()

    # ── UI construction ──────────────────────────────────────────────────────
    def _build_ui(self):
        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(18, 18, 18, 12)
        root_layout.setSpacing(14)
        self.setCentralWidget(root)

        # ── Header bar ─────────────────────────────────────────────
        header = QHBoxLayout()
        header.setSpacing(14)

        title_block = QVBoxLayout()
        title_block.setSpacing(2)
        title = QLabel("Cordyceps Query Studio")
        title.setProperty("role", "title")
        subtitle = QLabel("ENGRAMDB · INTERACTIVE QUERY WORKBENCH")
        subtitle.setProperty("role", "subtitle")
        title_block.addWidget(title)
        title_block.addWidget(subtitle)
        header.addLayout(title_block)
        header.addStretch(1)

        # Workspace picker
        ws_label = QLabel("WORKSPACE")
        ws_label.setProperty("role", "subtitle")
        ws_label.setAlignment(Qt.AlignmentFlag.AlignBottom)
        header.addWidget(ws_label, 0, Qt.AlignmentFlag.AlignBottom)

        self.ws_entry = QLineEdit(self.workspace_path)
        self.ws_entry.setMinimumWidth(380)
        self.ws_entry.returnPressed.connect(self._on_workspace_entered)
        header.addWidget(self.ws_entry, 1)

        self.browse_btn = QPushButton("Browse")
        self.browse_btn.clicked.connect(self.browse_workspace)
        header.addWidget(self.browse_btn)

        self.scan_btn = QPushButton("Scan / Build")
        self.scan_btn.setProperty("role", "primary")
        self.scan_btn.clicked.connect(self._start_scan)
        header.addWidget(self.scan_btn)

        root_layout.addLayout(header)

        # Divider
        divider = QFrame()
        divider.setProperty("role", "divider")
        divider.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        root_layout.addWidget(divider)

        # ── Query card ─────────────────────────────────────────────
        query_card = QFrame()
        query_card.setProperty("role", "card")
        qc_layout = make_card_layout(query_card)

        # Preset row
        preset_row = QHBoxLayout()
        preset_row.setSpacing(10)

        preset_label = QLabel("PRESET")
        preset_label.setProperty("role", "subtitle")
        preset_row.addWidget(preset_label)

        self.preset_combo = QComboBox()
        for label, _ in PRESET_QUERIES:
            self.preset_combo.addItem(label)
        self.preset_combo.setMinimumWidth(420)
        self.preset_combo.currentIndexChanged.connect(self._on_preset_selected)
        preset_row.addWidget(self.preset_combo)
        preset_row.addStretch(1)

        self.expand_var = QCheckBox("Expand Function Bodies")
        preset_row.addWidget(self.expand_var)

        qc_layout.addLayout(preset_row)

        # Query editor
        self.query_text = QPlainTextEdit()
        self.query_text.setPlaceholderText(
            "Enter a Query DSL statement — e.g.  GET functions WHERE is_async == true"
        )
        self.query_text.setMinimumHeight(78)
        self.query_text.setMaximumHeight(120)
        self.query_text.setPlainText(
            "GET functions WHERE is_async == true AND param_count >= 2"
        )
        qc_layout.addWidget(self.query_text)

        # Action row
        action_row = QHBoxLayout()
        action_row.setSpacing(10)

        self.run_btn = QPushButton("Execute Query")
        self.run_btn.setProperty("role", "primary")
        self.run_btn.clicked.connect(self.run_query)
        action_row.addWidget(self.run_btn)

        hint = QLabel("Ctrl + Enter")
        hint.setProperty("role", "subtitle")
        action_row.addWidget(hint)

        action_row.addStretch(1)

        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(lambda: self.query_text.clear())
        action_row.addWidget(clear_btn)

        qc_layout.addLayout(action_row)
        root_layout.addWidget(query_card)

        # ── Output tabs ────────────────────────────────────────────
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        root_layout.addWidget(self.tabs, 1)

        self._build_table_tab()
        self._build_yaml_tab()
        self._build_stats_tab()

        # Ctrl+Enter shortcut
        QShortcut(QKeySequence("Ctrl+Return"), self, activated=self.run_query)
        QShortcut(QKeySequence("Ctrl+Enter"), self, activated=self.run_query)

    def _build_table_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(0)

        self.tree = QTreeWidget()
        cols = (
            "name", "type", "file_path", "lines", "params",
            "is_async", "is_generator", "is_exported",
            "calls_count", "callers_count", "blast_radius_score",
        )
        self.tree.setColumnCount(len(cols))
        self.tree.setHeaderLabels([c.replace("_", " ").title() for c in cols])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setUniformRowHeights(True)
        self.tree.setSortingEnabled(True)
        self.tree.itemDoubleClicked.connect(self._on_row_double_click)

        header = self.tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setSectionsClickable(True)
        header.setSortIndicatorShown(True)
        # Better defaults
        widths = {
            0: 180, 1: 90, 2: 320, 3: 80, 4: 60, 5: 60,
            6: 60, 7: 80, 8: 60, 9: 70, 10: 100,
        }
        for idx, w in widths.items():
            self.tree.setColumnWidth(idx, w)

        layout.addWidget(self.tree)
        self.tabs.addTab(tab, "Table View")

    def _build_yaml_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        bar = QHBoxLayout()
        bar.addStretch(1)
        self.copy_yaml_btn = QPushButton("Copy YAML")
        self.copy_yaml_btn.clicked.connect(self._copy_yaml)
        bar.addWidget(self.copy_yaml_btn)
        layout.addLayout(bar)

        self.yaml_text = QPlainTextEdit()
        self.yaml_text.setReadOnly(True)
        self.yaml_text.setPlaceholderText("YAML output will appear here after a query.")
        layout.addWidget(self.yaml_text)

        self.tabs.addTab(tab, "YAML Output")

    def _build_stats_tab(self):
        tab = QWidget()
        outer = QVBoxLayout(tab)
        outer.setContentsMargins(14, 14, 14, 14)
        outer.setSpacing(14)

        # Stat tiles row
        tiles = QHBoxLayout()
        tiles.setSpacing(14)
        self.tile_nodes, self.tile_nodes_value = self._make_stat_tile("NODES")
        self.tile_scan,  self.tile_scan_value  = self._make_stat_tile("LAST SCAN (MS)")
        self.tile_query, self.tile_query_value = self._make_stat_tile("LAST QUERY (MS)")
        tiles.addWidget(self.tile_nodes)
        tiles.addWidget(self.tile_scan)
        tiles.addWidget(self.tile_query)
        outer.addLayout(tiles)

        # Detail card
        stats_card = QFrame()
        stats_card.setProperty("role", "card")
        sc_layout = make_card_layout(stats_card)
        sc_layout.addWidget(QLabel("ENGINE DETAILS"))
        self.stats_text = QPlainTextEdit()
        self.stats_text.setReadOnly(True)
        sc_layout.addWidget(self.stats_text)
        outer.addWidget(stats_card, 1)

        self.tabs.addTab(tab, "Graph Stats")

    def _make_stat_tile(self, label: str) -> tuple[QFrame, QLabel]:
        card = QFrame()
        card.setProperty("role", "card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(18, 14, 18, 14)
        lay.setSpacing(4)

        lab = QLabel(label)
        lab.setProperty("role", "stat")
        value = QLabel("—")
        value.setProperty("role", "statValue")
        lay.addWidget(lab)
        lay.addWidget(value)
        return card, value

    def _build_statusbar(self):
        sb = QStatusBar()
        self.setStatusBar(sb)
        self.status_label = QLabel("Status: Ready")
        self.status_label.setProperty("role", "subtitle")
        sb.addWidget(self.status_label)
        sb.showMessage("")

    # ── Actions ──────────────────────────────────────────────────────────────
    def browse_workspace(self):
        path = QFileDialog.getExistingDirectory(
            self, "Select Workspace", self.workspace_path
        )
        if path:
            self.ws_entry.setText(path)
            self._start_scan()

    def _on_workspace_entered(self):
        self._start_scan()

    def _start_scan(self):
        path = self.ws_entry.text().strip()
        if not os.path.isdir(path):
            QMessageBox.critical(self, "Error", f"Workspace path does not exist:\n{path}")
            return
        self.workspace_path = os.path.abspath(path)
        os.environ["WORKSPACE_PATH"] = self.workspace_path
        self._set_status("Status: Scanning workspace…")
        self.scan_btn.setEnabled(False)
        self.run_btn.setEnabled(False)

        if self._scan_worker and self._scan_worker.isRunning():
            return  # ignore overlapping scans

        self._scan_worker = ScanWorker(self.workspace_path)
        self._scan_worker.finished_ok.connect(self._on_scan_done)
        self._scan_worker.failed.connect(self._on_scan_failed)
        self._scan_worker.start()

    def _on_scan_done(self, elapsed_ms: float, stats: dict):
        # Re-fetch the DB singleton created inside the worker thread
        from src.database import get_graph_db
        self.db = get_graph_db(self.workspace_path)

        node_count = stats.get("nodes", 0)
        self._set_status(
            f"Status: Ready · Nodes: {node_count} · Scan: {elapsed_ms:.1f} ms"
        )
        self.tile_nodes_value.setText(str(node_count))
        self.tile_scan_value.setText(f"{elapsed_ms:.1f}")
        self._render_stats_text(stats, elapsed_ms)
        self.scan_btn.setEnabled(True)
        self.run_btn.setEnabled(True)

    def _on_scan_failed(self, msg: str):
        self._set_status("Status: Scan failed")
        self.scan_btn.setEnabled(True)
        self.run_btn.setEnabled(True)
        QMessageBox.critical(self, "Scan failed", msg)

    def _on_preset_selected(self, idx: int):
        if 0 <= idx < len(PRESET_QUERIES):
            self.query_text.setPlainText(PRESET_QUERIES[idx][1])
            self.run_query()

    def run_query(self):
        if not self.db:
            QMessageBox.warning(
                self, "Not ready", "Graph DB is still initializing. Please wait."
            )
            return

        raw_query = self.query_text.toPlainText().strip()
        if not raw_query:
            return

        self._set_status("Status: Executing query…")
        self.run_btn.setEnabled(False)

        if self._query_worker and self._query_worker.isRunning():
            return

        self._query_worker = QueryWorker(
            self.db.client, raw_query, self.expand_var.isChecked()
        )
        self._query_worker.finished_ok.connect(self._on_query_done)
        self._query_worker.failed.connect(self._on_query_failed)
        self._query_worker.start()

    def _on_query_done(self, res: dict, elapsed_ms: float):
        self.run_btn.setEnabled(True)
        self._display_results(res, elapsed_ms)
        self.tile_query_value.setText(f"{elapsed_ms:.2f}")

    def _on_query_failed(self, msg: str):
        self.run_btn.setEnabled(True)
        self._set_status("Status: Query failed")
        QMessageBox.critical(self, "Query failed", msg)

    # ── Rendering ────────────────────────────────────────────────────────────
    def _display_results(self, res: dict, elapsed_ms: float):
        self.current_results = res

        # 1) YAML
        yaml_str = yaml.dump(res, sort_keys=False, default_flow_style=False)
        self.yaml_text.setPlainText(yaml_str)

        # 2) Table
        self.tree.setSortingEnabled(False)
        self.tree.clear()

        meta = res.get("meta", {})
        raw_results = res.get("results")
        if isinstance(raw_results, dict):
            # Compact grouped format: {file_path: ["Name: start-end", ...]} or {file_path: "start-end"}
            items = []
            for fp, entries in raw_results.items():
                if isinstance(entries, str):
                    entries = [entries]
                for entry in entries:
                    if isinstance(entry, str) and ":" in entry:
                        name, _, lines = entry.partition(":")
                        items.append({"name": name, "file_path": fp, "lines_count": lines, "type": meta.get("type", "Node").capitalize()})
                    elif isinstance(entry, str):
                        items.append({"name": fp.split("/")[-1], "file_path": fp, "lines_count": entry, "type": meta.get("type", "Node").capitalize()})
                    elif isinstance(entry, dict):
                        entry["file_path"] = fp
                        items.append(entry)
        elif isinstance(raw_results, list):
            items = raw_results
        elif isinstance(raw_results, dict) and isinstance(raw_results.get("affected_nodes"), list):
            items = raw_results["affected_nodes"]  # IMPACT
        elif isinstance(raw_results, dict) and isinstance(raw_results.get("node"), dict):
            items = [raw_results["node"]]  # METADATA
        elif isinstance(raw_results, dict) and isinstance(raw_results.get("pipeline"), list):
            items = raw_results["pipeline"]  # FLOW_PIPELINE
        elif isinstance(res.get("impact"), dict):
            items = res["impact"].get("affected_nodes", [])
        elif isinstance(res.get("metadata"), dict):
            items = [res["metadata"]]
        elif isinstance(res.get("path"), list):
            items = [
                {
                    "name": p, "type": "PathNode",
                    "file_path": p.split(":")[0] if ":" in p else p,
                }
                for p in res["path"]
            ]
        elif isinstance(res.get("result"), dict):
            items = [res["result"]]
        else:
            items = []

        for item in items:
            if not isinstance(item, dict):
                continue

            name = item.get("name") or item.get("node_id") or ""
            ntype = item.get("type") or item.get("node_type") or "Function"
            file_path = item.get("file_path") or item.get("file") or ""

            lines_val = ""
            lines = item.get("lines") or item.get("defined_at_lines")
            if isinstance(lines, dict):
                lines_val = f"{lines.get('start', 0)}-{lines.get('end', 0)}"
            elif "lines_count" in item:
                lines_val = str(item["lines_count"])

            params = item.get("param_count", item.get("params", ""))
            is_async = self._bool_mark(item.get("is_async"))
            is_gen   = self._bool_mark(item.get("is_generator"))
            is_exp   = self._bool_mark(item.get("is_exported"))
            calls    = item.get(
                "calls_count",
                len(item.get("calls", [])) if item.get("calls") else ""
            )
            callers  = item.get(
                "callers_count",
                len(item.get("callers", [])) if item.get("callers") else ""
            )
            blast    = item.get("blast_radius_score", "")

            row = QTreeWidgetItem([
                str(name), str(ntype), str(file_path), str(lines_val),
                str(params), is_async, is_gen, is_exp,
                str(calls), str(callers), str(blast),
            ])
            self.tree.addTopLevelItem(row)

        self.tree.setSortingEnabled(True)

        # Status line
        count = len(items)
        if "meta" in res:
            total_matched = meta.get("total", count)
            offset = meta.get("offset", 0)
            remaining = max(0, total_matched - offset - count)
        else:
            total_matched = res.get("total_matched", count)
            remaining = res.get("remaining_count", 0)
        if remaining > 0:
            self._set_status(
                f"Status: Truncated · Showing {count} of {total_matched} "
                f"({remaining} remaining) · {elapsed_ms:.2f} ms"
            )
        else:
            self._set_status(
                f"Status: Success · Returned {count} item(s) · {elapsed_ms:.2f} ms"
            )

    def _render_stats_text(self, stats: dict, scan_ms: float):
        text = (
            "═══════════════════════════════════════════════\n"
            "  CORDYCEPS SEARCH — ENGRAMDB ENGINE STATS\n"
            "═══════════════════════════════════════════════\n"
            f"  Workspace Path : {self.workspace_path}\n"
            f"  Total Nodes    : {stats.get('nodes', 0)}\n"
            f"  Graph CSR State: {stats.get('edges', 'N/A')}\n"
            f"  Initial Scan   : {scan_ms:.2f} ms\n"
            "═══════════════════════════════════════════════\n"
        )
        self.stats_text.setPlainText(text)

    # ── Misc helpers ─────────────────────────────────────────────────────────
    @staticmethod
    def _bool_mark(v) -> str:
        if v is True:
            return "Yes"
        if v is False:
            return "—"
        return ""

    def _on_row_double_click(self, item: QTreeWidgetItem, _col: int):
        c = lambda i: item.text(i)  # noqa: E731
        detail = (
            f"Name      : {c(0)}\n"
            f"Type      : {c(1)}\n"
            f"File Path : {c(2)}\n"
            f"Lines     : {c(3)}\n"
            f"Params    : {c(4)}\n"
            f"Async     : {c(5)}\n"
            f"Generator : {c(6)}\n"
            f"Exported  : {c(7)}\n"
            f"Calls     : {c(8)}\n"
            f"Callers   : {c(9)}\n"
            f"Blast     : {c(10)}"
        )
        QMessageBox.information(self, "Node Details", detail)

    def _copy_yaml(self):
        text = self.yaml_text.toPlainText()
        QGuiApplication.clipboard().setText(text)
        self._set_status("Status: YAML copied to clipboard")

    def _set_status(self, text: str):
        self.status_label.setText(text)


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────
def main():
    # High-DPI: opt into crisp rendering on Windows / Linux / macOS
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setApplicationName("Cordyceps Query Studio")
    app.setOrganizationName("Cordyceps Search")
    app.setStyleSheet(STYLESHEET)
    window = CordycepsQueryStudio()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()