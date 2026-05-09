"""
Clipboard History — Windows 剪贴板历史管理工具
基于 PySide6 + SQLite
"""
import sys
import os
import json
import time
import sqlite3
import base64
import ctypes
from ctypes import wintypes
import re
from datetime import datetime, timedelta

# Windows MSG structure for nativeEvent
class WinMSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt_x", wintypes.LONG),
        ("pt_y", wintypes.LONG),
    ]

from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLineEdit,
    QPushButton, QScrollArea, QFrame, QLabel, QSystemTrayIcon,
    QMenu, QComboBox, QDialog, QListWidget, QListWidgetItem,
    QMessageBox, QSizePolicy
)
from PySide6.QtCore import (
    Qt, QTimer, Signal, Slot, QSize, QEvent, QPoint, QObject,
    QBuffer, QByteArray, QIODevice
)
from PySide6.QtNetwork import QLocalSocket
from PySide6.QtGui import (
    QAction, QIcon, QPixmap, QImage, QPainter, QColor, QFont,
    QPalette, QShortcut, QKeySequence, QCursor
)

# ─── Configuration ───────────────────────────────────────────
APP_NAME = "Clipboard History"
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DB_PATH = os.path.join(DATA_DIR, "clips.db")
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")
DEFAULT_SETTINGS = {
    "retention_days": 3,
    "excluded_apps": [],
    "max_clips": 2000,
    "poll_interval_ms": 500
}

# ─── Color Theme ──────────────────────────────────────────────
COLORS = {
    "primary": "#A8D8EA",
    "primary_hover": "#7EC8E3",
    "primary_border": "#C5E0EB",
    "bg": "#F5F9FB",
    "card_bg": "#FFFFFF",
    "text": "#333333",
    "text_secondary": "#888888",
    "pin": "#FFB347",
    "delete": "#E88B8B",
    "delete_hover": "#D46A6A",
}

os.makedirs(DATA_DIR, exist_ok=True)


# ─── Database ─────────────────────────────────────────────────
class Database:
    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._init_tables()

    def _init_tables(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS clips (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp REAL NOT NULL,
                pinned INTEGER DEFAULT 0,
                source_app TEXT
            )
        """)
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_ts ON clips(timestamp DESC)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_type ON clips(type)")
        self.conn.commit()

    def insert(self, clip_type, content, source_app=""):
        self.conn.execute(
            "INSERT INTO clips (type, content, timestamp, source_app) VALUES (?, ?, ?, ?)",
            (clip_type, content, time.time(), source_app)
        )
        self.conn.commit()
        return self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    def get_all(self, search=""):
        if search:
            return self.conn.execute(
                "SELECT * FROM clips WHERE type='text' AND content LIKE ? "
                "ORDER BY pinned DESC, timestamp DESC LIMIT 500",
                (f"%{search}%",)
            ).fetchall()
        return self.conn.execute(
            "SELECT * FROM clips ORDER BY pinned DESC, timestamp DESC LIMIT 500"
        ).fetchall()

    def delete(self, clip_id):
        self.conn.execute("DELETE FROM clips WHERE id=?", (clip_id,))
        self.conn.commit()

    def toggle_pin(self, clip_id):
        row = self.conn.execute("SELECT pinned FROM clips WHERE id=?", (clip_id,)).fetchone()
        if row:
            new_val = 0 if row[0] else 1
            self.conn.execute("UPDATE clips SET pinned=? WHERE id=?", (new_val, clip_id))
            self.conn.commit()
            return new_val
        return 0

    def clean_expired(self, retention_days):
        cutoff = time.time() - (retention_days * 86400)
        self.conn.execute("DELETE FROM clips WHERE pinned=0 AND timestamp < ?", (cutoff,))
        self.conn.commit()

    def get_count(self):
        return self.conn.execute("SELECT COUNT(*) FROM clips").fetchone()[0]


# ─── Settings ─────────────────────────────────────────────────
class Settings:
    def __init__(self):
        self.data = dict(DEFAULT_SETTINGS)
        self.load()

    def load(self):
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                self.data.update(json.load(f))
        except (FileNotFoundError, json.JSONDecodeError):
            self.save()

    def save(self):
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2)

    def get(self, key, default=None):
        if default is None:
            return self.data.get(key, DEFAULT_SETTINGS.get(key))
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value
        self.save()


# ─── Clipboard Card Widget ────────────────────────────────────
class ClipCard(QFrame):
    clicked = Signal(int)
    delete_requested = Signal(int)
    pin_requested = Signal(int)

    def __init__(self, clip_data):
        super().__init__()
        self.clip_id = clip_data[0]
        self.clip_type = clip_data[1]
        self.content = clip_data[2]
        self.timestamp = clip_data[3]
        self.pinned = bool(clip_data[4])

        self.setObjectName("clipCard")
        self.setCursor(Qt.PointingHandCursor)
        self._setup_ui()

    def _setup_ui(self):
        self.setFixedHeight(70)
        self.setStyleSheet(self._card_style())

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(10)

        # Pin indicator
        self.pin_label = QLabel("📌" if self.pinned else "")
        self.pin_label.setFixedWidth(20)
        self.pin_label.setStyleSheet("font-size: 14px; color: #FFB347;")
        layout.addWidget(self.pin_label)

        # Content area
        content_widget = QWidget()
        content_layout = QVBoxLayout(content_widget)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(4)

        if self.clip_type == "text":
            text = self.content[:150].replace("\n", " ")
            self.text_label = QLabel(text)
            self.text_label.setWordWrap(True)
            self.text_label.setStyleSheet("color: #333; font-size: 13px;")
            self.text_label.setMaximumHeight(36)
            content_layout.addWidget(self.text_label)
        else:
            # Image thumbnail
            try:
                img_data = base64.b64decode(self.content)
                img_buf = QBuffer()
                img_buf.open(QIODevice.ReadOnly)
                img_buf.write(img_data)
                img_buf.seek(0)
                image = QImage()
                image.load(img_buf, "PNG")
                img_buf.close()
                pixmap = QPixmap.fromImage(image)
                thumb = pixmap.scaled(40, 40, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                self.thumb_label = QLabel()
                self.thumb_label.setPixmap(thumb)
                self.thumb_label.setFixedSize(40, 40)
                self.thumb_label.setStyleSheet("border: 1px solid #EEE; border-radius: 4px;")
                content_layout.addWidget(self.thumb_label)
            except Exception:
                self.text_label = QLabel("[图片]")
                content_layout.addWidget(self.text_label)

        # Timestamp
        ts = datetime.fromtimestamp(self.timestamp)
        time_str = self._format_time(ts)
        self.time_label = QLabel(time_str)
        self.time_label.setStyleSheet("color: #888; font-size: 11px;")
        content_layout.addWidget(self.time_label)

        layout.addWidget(content_widget, 1)

        # Action buttons
        actions_widget = QWidget()
        actions_layout = QHBoxLayout(actions_widget)
        actions_layout.setContentsMargins(0, 0, 0, 0)
        actions_layout.setSpacing(6)

        pin_btn = QPushButton("📌")
        pin_btn.setFixedSize(28, 28)
        pin_btn.setStyleSheet(self._btn_style("#FFF3E0", "#FFB347"))
        pin_btn.setCursor(Qt.PointingHandCursor)
        pin_btn.clicked.connect(lambda e: self.pin_requested.emit(self.clip_id))
        pin_btn.setToolTip("置顶/取消置顶")

        del_btn = QPushButton("🗑")
        del_btn.setFixedSize(28, 28)
        del_btn.setStyleSheet(self._btn_style("#FFEBEE", "#E88B8B"))
        del_btn.setCursor(Qt.PointingHandCursor)
        del_btn.clicked.connect(lambda e: self.delete_requested.emit(self.clip_id))
        del_btn.setToolTip("删除")

        actions_layout.addWidget(pin_btn)
        actions_layout.addWidget(del_btn)
        layout.addWidget(actions_widget)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self.clip_id)
        super().mousePressEvent(event)

    def _card_style(self):
        border = f"border-left: 3px solid {COLORS['pin']};" if self.pinned else ""
        return f"""
            QFrame#clipCard {{
                background: {COLORS['card_bg']};
                border-radius: 8px;
                border: 1px solid transparent;
                {border}
            }}
            QFrame#clipCard:hover {{
                border-color: {COLORS['primary_hover']};
                background: #FAFDFF;
            }}
        """

    def _btn_style(self, bg_hover, text_color):
        return f"""
            QPushButton {{
                background: transparent;
                border: none;
                border-radius: 4px;
                font-size: 13px;
            }}
            QPushButton:hover {{
                background: {bg_hover};
                color: {text_color};
            }}
        """

    def _format_time(self, ts):
        now = datetime.now()
        diff = now - ts
        if diff < timedelta(minutes=1):
            return "刚刚"
        elif diff < timedelta(hours=1):
            return f"{int(diff.total_seconds() / 60)}分钟前"
        elif diff < timedelta(days=1):
            return ts.strftime("%H:%M")
        elif diff < timedelta(days=7):
            return f"{diff.days}天前"
        else:
            return ts.strftime("%m/%d %H:%M")


# ─── Settings Dialog ──────────────────────────────────────────
class SettingsDialog(QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("设置")
        self.setFixedSize(360, 420)
        self.setWindowFlags(Qt.Dialog | Qt.WindowCloseButtonHint)
        self._setup_ui()
        self.setStyleSheet(f"""
            QDialog {{ background: {COLORS['bg']}; }}
            QLabel {{ color: {COLORS['text']}; font-size: 13px; }}
            QPushButton {{
                background: {COLORS['primary']}; border: none;
                border-radius: 6px; padding: 8px 16px; font-size: 13px;
            }}
            QPushButton:hover {{ background: {COLORS['primary_hover']}; }}
            QComboBox {{
                background: white; border: 1px solid {COLORS['primary_border']};
                border-radius: 6px; padding: 6px 10px; font-size: 13px;
            }}
            QListWidget {{
                background: white; border: 1px solid {COLORS['primary_border']};
                border-radius: 6px; font-size: 13px;
            }}
        """)

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(16)
        layout.setContentsMargins(24, 24, 24, 24)

        # Retention period
        layout.addWidget(QLabel("保留期限"))
        self.retention_combo = QComboBox()
        self.retention_combo.addItems(["1 天", "3 天", "5 天"])
        idx = {1: 0, 3: 1, 5: 2}.get(self.settings.get("retention_days"), 1)
        self.retention_combo.setCurrentIndex(idx)
        layout.addWidget(self.retention_combo)

        # Excluded apps
        layout.addWidget(QLabel("排除应用（进程名，如 notepad.exe）"))
        add_layout = QHBoxLayout()
        self.app_input = QLineEdit()
        self.app_input.setPlaceholderText("输入进程名...")
        self.app_input.setStyleSheet("""
            QLineEdit {
                background: white; border: 1px solid #C5E0EB;
                border-radius: 6px; padding: 6px 10px; font-size: 13px;
            }
        """)
        add_btn = QPushButton("添加")
        add_btn.clicked.connect(self._add_app)
        add_layout.addWidget(self.app_input)
        add_layout.addWidget(add_btn)
        layout.addLayout(add_layout)

        self.app_list = QListWidget()
        for app in self.settings.get("excluded_apps", []):
            self.app_list.addItem(app)
        layout.addWidget(self.app_list)

        remove_btn = QPushButton("移除选中")
        remove_btn.clicked.connect(self._remove_app)
        layout.addWidget(remove_btn)

        # Save button
        save_btn = QPushButton("保存设置")
        save_btn.setStyleSheet(f"""
            QPushButton {{
                background: {COLORS['primary_hover']};
                color: white; font-weight: bold;
                border: none; border-radius: 6px;
                padding: 10px; font-size: 14px;
            }}
            QPushButton:hover {{ background: #5BA3C0; }}
        """)
        save_btn.clicked.connect(self._save)
        layout.addWidget(save_btn)

    def _add_app(self):
        name = self.app_input.text().strip().lower()
        if name:
            self.app_list.addItem(name)
            self.app_input.clear()

    def _remove_app(self):
        for item in self.app_list.selectedItems():
            self.app_list.takeItem(self.app_list.row(item))

    def _save(self):
        days_map = {0: 1, 1: 3, 2: 5}
        self.settings.set("retention_days", days_map[self.retention_combo.currentIndex()])
        apps = []
        for i in range(self.app_list.count()):
            apps.append(self.app_list.item(i).text())
        self.settings.set("excluded_apps", apps)
        self.accept()


# ─── Main Window ──────────────────────────────────────────────
class MainWindow(QWidget):
    WM_HOTKEY = 0x0312
    HOTKEY_ID = 1

    def __init__(self, db, settings):
        super().__init__()
        self.db = db
        self.settings = settings
        self.clips = []

        self.setWindowTitle(APP_NAME)
        self.setFixedSize(400, 560)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)

        self._setup_ui()
        self._apply_theme()
        self.load_clips()

        # Shortcut: Escape to hide
        QShortcut(QKeySequence(Qt.Key_Escape), self, self.hide)
        QShortcut(QKeySequence("Ctrl+F"), self, lambda: self.search_input.setFocus())

        # Register Alt+V global hotkey on this window
        self._register_hotkey()

    def _register_hotkey(self):
        try:
            hwnd = int(self.winId())
            MOD_ALT = 0x0001
            VK_V = 0x56
            result = ctypes.windll.user32.RegisterHotKey(hwnd, self.HOTKEY_ID, MOD_ALT, VK_V)
            if result:
                print("Alt+V hotkey registered successfully on HWND:", hwnd)
            else:
                print("Alt+V registration failed (error:", ctypes.get_last_error(), ")")
        except Exception as e:
            print("Hotkey setup error:", e)

    def nativeEvent(self, eventType, message):
        msg = WinMSG.from_address(int(message))
        if msg.message == self.WM_HOTKEY and msg.wParam == self.HOTKEY_ID:
            self._toggle_show()
            return True, 0
        return False, 0

    def _toggle_show(self):
        if self.isVisible():
            self.hide()
        else:
            cursor_pos = QCursor.pos()
            screen = QApplication.screenAt(cursor_pos) or QApplication.primaryScreen()
            screen_geo = screen.availableGeometry()
            self.move(screen_geo.right() - 420, screen_geo.top() + 20)
            self.show()
            self.activateWindow()
            self.raise_()
            self.search_input.setFocus()
            self.search_input.clear()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        # Title bar (drag handle)
        title_bar = QWidget()
        title_layout = QHBoxLayout(title_bar)
        title_layout.setContentsMargins(4, 0, 4, 0)

        title_label = QLabel("📋 剪贴板历史")
        title_label.setStyleSheet(f"color: {COLORS['text']}; font-size: 15px; font-weight: bold;")
        title_layout.addWidget(title_label)
        title_layout.addStretch()

        settings_btn = QPushButton("⚙")
        settings_btn.setFixedSize(30, 30)
        settings_btn.setCursor(Qt.PointingHandCursor)
        settings_btn.setStyleSheet(self._icon_btn_style())
        settings_btn.clicked.connect(self._open_settings)
        title_layout.addWidget(settings_btn)

        close_btn = QPushButton("✕")
        close_btn.setFixedSize(30, 30)
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.setStyleSheet(self._icon_btn_style())
        close_btn.clicked.connect(self._toggle_show)
        title_layout.addWidget(close_btn)

        layout.addWidget(title_bar)

        # Search bar
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("🔍 搜索复制历史...")
        self.search_input.setStyleSheet(f"""
            QLineEdit {{
                background: white;
                border: 1.5px solid {COLORS['primary_border']};
                border-radius: 8px;
                padding: 10px 14px;
                font-size: 14px;
                color: {COLORS['text']};
            }}
            QLineEdit:focus {{ border-color: {COLORS['primary_hover']}; }}
        """)
        self.search_input.textChanged.connect(self._on_search)
        layout.addWidget(self.search_input)

        # Toolbar
        toolbar = QWidget()
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(0, 0, 0, 0)
        toolbar_layout.setSpacing(8)

        self.retention_combo = QComboBox()
        self.retention_combo.addItems(["保留 1 天", "保留 3 天", "保留 5 天"])
        idx = {1: 0, 3: 1, 5: 2}.get(self.settings.get("retention_days"), 1)
        self.retention_combo.setCurrentIndex(idx)
        self.retention_combo.setStyleSheet(f"""
            QComboBox {{
                background: white; border: 1px solid {COLORS['primary_border']};
                border-radius: 6px; padding: 6px 10px; font-size: 12px;
                color: {COLORS['text']};
            }}
            QComboBox:hover {{ background: {COLORS['primary']}; }}
        """)
        self.retention_combo.currentIndexChanged.connect(self._on_retention_change)
        toolbar_layout.addWidget(self.retention_combo)

        clean_btn = QPushButton("清理过期")
        clean_btn.setStyleSheet(f"""
            QPushButton {{
                background: white; border: 1px solid {COLORS['primary_border']};
                border-radius: 6px; padding: 6px 12px; font-size: 12px;
                color: {COLORS['text']};
            }}
            QPushButton:hover {{ background: {COLORS['delete']}; color: white; }}
        """)
        clean_btn.setCursor(Qt.PointingHandCursor)
        clean_btn.clicked.connect(self._clean_expired)
        toolbar_layout.addWidget(clean_btn)

        toolbar_layout.addStretch()
        self.count_label = QLabel()
        self.count_label.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 12px;")
        toolbar_layout.addWidget(self.count_label)
        layout.addWidget(toolbar)

        # Scroll area for cards
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet(f"""
            QScrollArea {{ background: transparent; border: none; }}
            QScrollBar:vertical {{
                width: 5px; background: transparent;
            }}
            QScrollBar::handle:vertical {{
                background: {COLORS['primary_border']}; border-radius: 10px;
            }}
        """)

        self.card_container = QWidget()
        self.card_layout = QVBoxLayout(self.card_container)
        self.card_layout.setContentsMargins(4, 4, 4, 4)
        self.card_layout.setSpacing(6)
        self.card_layout.addStretch()

        scroll.setWidget(self.card_container)
        layout.addWidget(scroll, 1)

    def _apply_theme(self):
        self.setStyleSheet(f"""
            QWidget {{
                background: {COLORS['bg']};
                font-family: "Microsoft YaHei", sans-serif;
            }}
        """)

    def load_clips(self, search=""):
        # Clear existing cards
        for i in reversed(range(self.card_layout.count())):
            item = self.card_layout.itemAt(i)
            if item.widget():
                item.widget().deleteLater()
            elif item.spacerItem():
                self.card_layout.removeItem(item)

        self.clips = self.db.get_all(search)

        for clip in self.clips:
            card = ClipCard(clip)
            card.clicked.connect(self._on_clip_click)
            card.delete_requested.connect(self._on_delete)
            card.pin_requested.connect(self._on_pin)
            self.card_layout.insertWidget(self.card_layout.count() - 1, card)

        self.card_layout.addStretch()
        self.count_label.setText(f"共 {len(self.clips)} 条")

    @Slot(str)
    def _on_search(self, text):
        self.load_clips(text)

    @Slot(int)
    def _on_clip_click(self, clip_id):
        # Find the clip and paste
        for clip in self.clips:
            if clip[0] == clip_id:
                clipboard = QApplication.clipboard()
                if clip[1] == "text":
                    clipboard.setText(clip[2])
                else:
                    try:
                        img_data = base64.b64decode(clip[2])
                        img_buf = QBuffer()
                        img_buf.open(QIODevice.ReadOnly)
                        img_buf.write(img_data)
                        img_buf.seek(0)
                        image = QImage()
                        image.load(img_buf, "PNG")
                        img_buf.close()
                        clipboard.setImage(image)
                    except Exception:
                        pass
                break

        # Simulate Ctrl+V to paste to active window
        self.hide()
        QTimer.singleShot(150, self._simulate_paste)

    def _simulate_paste(self):
        # Simulate Ctrl+V key press
        VK_CONTROL = 0x11
        VK_V = 0x56
        KEYEVENTF_KEYUP = 0x0002

        user32 = ctypes.windll.user32
        user32.keybd_event(VK_CONTROL, 0, 0, 0)
        user32.keybd_event(VK_V, 0, 0, 0)
        user32.keybd_event(VK_V, 0, KEYEVENTF_KEYUP, 0)
        user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)

    @Slot(int)
    def _on_delete(self, clip_id):
        self.db.delete(clip_id)
        self.load_clips(self.search_input.text())

    @Slot(int)
    def _on_pin(self, clip_id):
        self.db.toggle_pin(clip_id)
        self.load_clips(self.search_input.text())

    def _on_retention_change(self):
        days_map = {0: 1, 1: 3, 2: 5}
        days = days_map[self.retention_combo.currentIndex()]
        self.settings.set("retention_days", days)
        self.db.clean_expired(days)
        self.load_clips(self.search_input.text())

    def _clean_expired(self):
        days = self.settings.get("retention_days")
        self.db.clean_expired(days)
        self.load_clips(self.search_input.text())

    def _open_settings(self):
        dialog = SettingsDialog(self.settings, self)
        if dialog.exec() == QDialog.Accepted:
            # Update retention combo
            days = self.settings.get("retention_days")
            idx = {1: 0, 3: 1, 5: 2}.get(days, 1)
            self.retention_combo.setCurrentIndex(idx)
            self.load_clips(self.search_input.text())

    def _icon_btn_style(self):
        return f"""
            QPushButton {{
                background: transparent; border: none;
                border-radius: 6px; font-size: 16px;
                color: {COLORS['text_secondary']};
            }}
            QPushButton:hover {{
                background: {COLORS['primary']}; color: {COLORS['text']};
            }}
        """

    def add_clip(self, clip_data):
        """Called from clipboard monitor when new clip is detected"""
        excluded = [a.lower() for a in self.settings.get("excluded_apps", [])]
        source = clip_data.get("source_app", "").lower()
        if source and any(e in source for e in excluded):
            return

        self.db.insert(clip_data["type"], clip_data["content"], source)
        # Reload if window is visible
        if self.isVisible():
            self.load_clips(self.search_input.text())

    def focusOutEvent(self, event):
        # Don't hide immediately for child dialogs
        pass


# ─── Clipboard Monitor ───────────────────────────────────────
class ClipboardMonitor(QObject):
    new_clip = Signal(dict)

    def __init__(self, settings):
        super().__init__()
        self.settings = settings
        self.last_text = ""
        self.last_image_hash = ""

        # Initialize clipboard
        clipboard = QApplication.clipboard()
        if clipboard.text():
            self.last_text = clipboard.text()

    def check(self):
        clipboard = QApplication.clipboard()
        mime = clipboard.mimeData()

        # Check text
        if mime.hasText():
            text = mime.text()
            if text and text != self.last_text:
                self.last_text = text
                self.new_clip.emit({
                    "type": "text",
                    "content": text,
                    "source_app": self._get_active_window_process()
                })
                return

        # Check image
        if mime.hasImage():
            image = clipboard.image()
            if not image.isNull():
                # Generate a simple hash to detect changes
                img_hash = str(image.cacheKey())
                if img_hash != self.last_image_hash:
                    self.last_image_hash = img_hash

                    # Compress to thumbnail and base64
                    pixmap = QPixmap.fromImage(image)
                    thumb = pixmap.scaled(200, 200, Qt.KeepAspectRatio, Qt.SmoothTransformation)

                    buf = QBuffer()
                    buf.open(QIODevice.WriteOnly)
                    thumb.save(buf, "PNG")
                    buf.close()
                    b64 = base64.b64encode(buf.data()).decode("utf-8")

                    self.new_clip.emit({
                        "type": "image",
                        "content": b64,
                        "source_app": self._get_active_window_process()
                    })

    def _get_active_window_process(self):
        try:
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            psapi = ctypes.windll.psapi

            hwnd = user32.GetForegroundWindow()
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))

            process_handle = kernel32.OpenProcess(0x0400 | 0x0010, False, pid.value)
            if process_handle:
                name_buf = ctypes.create_unicode_buffer(260)
                psapi.GetModuleBaseNameW(process_handle, None, name_buf, 260)
                kernel32.CloseHandle(process_handle)
                return name_buf.value
        except Exception:
            pass
        return ""


# ─── System Tray ──────────────────────────────────────────────
def create_tray_icon(app, window):
    # Create a simple icon
    pixmap = QPixmap(32, 32)
    pixmap.fill(QColor(COLORS["primary"]))
    painter = QPainter(pixmap)
    painter.setPen(QColor("#FFFFFF"))
    painter.setFont(QFont("Microsoft YaHei", 16))
    painter.drawText(pixmap.rect(), Qt.AlignCenter, "📋")
    painter.end()
    icon = QIcon(pixmap)

    tray = QSystemTrayIcon(icon, app)
    tray.setToolTip(APP_NAME)

    menu = QMenu()
    menu.setStyleSheet(f"""
        QMenu {{
            background: white; border: 1px solid {COLORS['primary_border']};
            border-radius: 8px; padding: 4px;
        }}
        QMenu::item {{
            padding: 8px 24px; font-size: 13px; color: {COLORS['text']};
        }}
        QMenu::item:selected {{ background: {COLORS['primary']}; }}
    """)

    show_action = QAction("显示/隐藏")
    show_action.triggered.connect(window._toggle_show)
    menu.addAction(show_action)

    menu.addSeparator()

    quit_action = QAction("退出")
    quit_action.triggered.connect(app.quit)
    menu.addAction(quit_action)

    tray.setContextMenu(menu)
    tray.activated.connect(lambda reason: window._toggle_show() if reason == QSystemTrayIcon.DoubleClick else None)

    tray.show()
    return tray


# ─── Main ─────────────────────────────────────────────────────
def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    # Prevent multiple instances
    socket = QLocalSocket()
    socket.connectToServer(APP_NAME)
    if socket.waitForConnected(500):
        print("Another instance is already running.")
        sys.exit(0)

    db = Database()
    settings = Settings()

    # Clean expired on startup
    db.clean_expired(settings.get("retention_days"))

    # Create main window
    window = MainWindow(db, settings)

    # System tray
    tray = create_tray_icon(app, window)

    # Clipboard monitor
    monitor = ClipboardMonitor(settings)
    monitor.new_clip.connect(window.add_clip)

    timer = QTimer()
    timer.timeout.connect(monitor.check)
    timer.start(settings.get("poll_interval_ms", 500))

    # Show window at start
    window._toggle_show()

    # Show welcome message
    tray.showMessage(APP_NAME, "剪贴板历史记录已开始运行\nAlt+V 呼出窗口", QSystemTrayIcon.Information, 2000)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
