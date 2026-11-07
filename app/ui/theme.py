"""整体黑白（单色）主题。

设计约定
--------
* 只有白 / 灰 / 黑三档：白底、浅灰分区、深灰边框、黑字；
* 选中态用「黑底白字」反色，一眼能看出选中的行/列/单元格；
* 状态区分靠 灰度 + 粗细 + 符号（✓ / ✗ / ⚙），不靠红绿蓝；
* 表格里"列颜色"的快速配色也只给灰度色阶，需要彩色时仍可在取色器里自选。

改这里就能整体换肤；界面代码一律引用本模块的常量，不再写死颜色值。
"""
from __future__ import annotations

# ---------------------------------------------- 基础色 ---------------------------------------------- #
FG = "#111111"          # 正文
FG_STRONG = "#000000"   # 强调（黑、加粗）
FG_DIM = "#8A8A8A"      # 次要说明
BG = "#FFFFFF"          # 主背景
BG_ALT = "#F4F4F4"      # 交替行 / 次级背景
BG_PANEL = "#FAFAFA"    # 面板背景
BG_SUNKEN = "#EFEFEF"   # 输入框 / 文本框
BORDER = "#BFBFBF"      # 边框
BORDER_DARK = "#8C8C8C"  # 强调边框
SEL_BG = "#111111"      # 选中背景（反色）
SEL_FG = "#FFFFFF"      # 选中文字
DISABLED_FG = "#A6A6A6"

# ---------------------------------------------- 语义色（全部灰度） ---------------------------------------------- #
OK_FG = "#222222"       # 通过
FAIL_FG = "#000000"     # 失败（黑 + 加粗 + ✗）
NA_FG = "#9A9A9A"       # 不适用
ERROR_BG = "#E4E4E4"    # 出错单元格背景
CHANGED_BG = "#DCDCDC"  # 被编辑/有差异的单元格背景
BIT_SET_FG = "#000000"  # 位 = 1
BIT_CLR_FG = "#B5B5B5"  # 位 = 0

#: 列「快速配色」色阶（纯灰度）
COLUMN_COLORS = [
    "#F7F7F7", "#EFEFEF", "#E6E6E6", "#DCDCDC", "#D2D2D2",
    "#C8C8C8", "#BEBEBE", "#B4B4B4", "#AAAAAA",
]

MONO_FAMILIES = ["Consolas", "Cascadia Mono", "DejaVu Sans Mono", "Courier New"]

# ---------------------------------------------- 样式表 ---------------------------------------------- #
MONO_QSS = f"""
QWidget {{
    color: {FG};
    background-color: {BG};
}}
QMainWindow, QDialog {{ background-color: {BG}; }}

QMenuBar {{ background-color: {BG_PANEL}; border-bottom: 1px solid {BORDER}; }}
QMenuBar::item {{ padding: 4px 10px; background: transparent; }}
QMenuBar::item:selected {{ background-color: {SEL_BG}; color: {SEL_FG}; }}
QMenu {{ background-color: {BG}; border: 1px solid {BORDER}; }}
QMenu::item {{ padding: 4px 22px 4px 22px; }}
QMenu::item:selected {{ background-color: {SEL_BG}; color: {SEL_FG}; }}
QMenu::item:disabled {{ color: {DISABLED_FG}; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 3px 6px; }}

QToolBar {{ background-color: {BG_PANEL}; border-bottom: 1px solid {BORDER}; spacing: 2px; padding: 2px; }}
QToolButton {{ padding: 4px 8px; border: 1px solid transparent; }}
QToolButton:hover {{ border: 1px solid {BORDER_DARK}; background-color: {BG_ALT}; }}
QToolButton:checked {{ background-color: {SEL_BG}; color: {SEL_FG}; border: 1px solid {SEL_BG}; }}

QPushButton {{
    background-color: {BG_PANEL};
    border: 1px solid {BORDER};
    padding: 4px 10px;
    min-height: 18px;
}}
QPushButton:hover {{ background-color: {BG_ALT}; border-color: {BORDER_DARK}; }}
QPushButton:pressed {{ background-color: {SEL_BG}; color: {SEL_FG}; border-color: {SEL_BG}; }}
QPushButton:checked {{ background-color: {SEL_BG}; color: {SEL_FG}; }}
QPushButton:disabled {{ color: {DISABLED_FG}; border-color: {BORDER}; background-color: {BG_ALT}; }}

QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background-color: {BG_SUNKEN};
    border: 1px solid {BORDER};
    selection-background-color: {SEL_BG};
    selection-color: {SEL_FG};
    padding: 2px 4px;
}}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QSpinBox:focus,
QDoubleSpinBox:focus, QComboBox:focus {{ border-color: {BORDER_DARK}; background-color: {BG}; }}
QComboBox::drop-down {{ width: 16px; border-left: 1px solid {BORDER}; }}
QComboBox QAbstractItemView {{
    background-color: {BG};
    border: 1px solid {BORDER};
    selection-background-color: {SEL_BG};
    selection-color: {SEL_FG};
}}

QCheckBox, QRadioButton {{ spacing: 6px; }}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 13px; height: 13px;
    border: 1px solid {BORDER_DARK};
    background-color: {BG};
}}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
    background-color: {SEL_BG};
    border-color: {SEL_BG};
}}
QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {{ border-color: {BORDER}; }}

QGroupBox {{
    border: 1px solid {BORDER};
    margin-top: 12px;
    padding-top: 6px;
    background-color: {BG_PANEL};
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 8px;
    padding: 0 4px;
    color: {FG_STRONG};
    font-weight: bold;
}}

QTabWidget::pane {{ border: 1px solid {BORDER}; background-color: {BG}; }}
QTabBar::tab {{
    background-color: {BG_ALT};
    border: 1px solid {BORDER};
    border-bottom: none;
    padding: 4px 12px;
    margin-right: 1px;
}}
QTabBar::tab:selected {{ background-color: {BG}; color: {FG_STRONG}; font-weight: bold; }}
QTabBar::tab:hover {{ background-color: {BG_PANEL}; }}

QHeaderView::section {{
    background-color: {BG_ALT};
    color: {FG_STRONG};
    padding: 3px 6px;
    border: none;
    border-right: 1px solid {BORDER};
    border-bottom: 1px solid {BORDER};
    font-weight: bold;
}}
QHeaderView::section:hover {{ background-color: {CHANGED_BG}; }}
QTableCornerButton::section {{ background-color: {BG_ALT}; border: 1px solid {BORDER}; }}
QTableView, QTableWidget {{
    background-color: {BG};
    alternate-background-color: {BG_ALT};
    gridline-color: {BORDER};
    border: 1px solid {BORDER};
    selection-background-color: {SEL_BG};
    selection-color: {SEL_FG};
}}
QTableView::item:selected, QTableWidget::item:selected {{
    background-color: {SEL_BG};
    color: {SEL_FG};
}}

QSplitter::handle {{ background-color: {BORDER}; }}
QSplitter::handle:horizontal {{ width: 3px; }}
QSplitter::handle:vertical {{ height: 3px; }}

QScrollBar:vertical {{ background: {BG_ALT}; width: 12px; margin: 0; }}
QScrollBar:horizontal {{ background: {BG_ALT}; height: 12px; margin: 0; }}
QScrollBar::handle {{ background: {BORDER_DARK}; min-height: 24px; min-width: 24px; }}
QScrollBar::handle:hover {{ background: {FG_DIM}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: {BG_ALT}; }}

QStatusBar {{ background-color: {BG_PANEL}; border-top: 1px solid {BORDER}; }}
QStatusBar::item {{ border: none; }}
QLabel {{ background: transparent; }}

QToolTip {{
    background-color: {SEL_BG};
    color: {SEL_FG};
    border: 1px solid {BORDER_DARK};
    padding: 4px 6px;
}}
QProgressBar {{ border: 1px solid {BORDER}; background: {BG_ALT}; text-align: center; }}
QProgressBar::chunk {{ background-color: {BORDER_DARK}; }}
"""


def apply_theme(app) -> None:
    """给 QApplication 套上黑白主题（Fusion 风格 + 灰度调色板 + 样式表）。"""
    from PyQt6.QtGui import QColor, QPalette

    app.setStyle("Fusion")
    palette = QPalette()
    role = QPalette.ColorRole
    group = QPalette.ColorGroup

    palette.setColor(role.Window, QColor(BG))
    palette.setColor(role.WindowText, QColor(FG))
    palette.setColor(role.Base, QColor(BG))
    palette.setColor(role.AlternateBase, QColor(BG_ALT))
    palette.setColor(role.ToolTipBase, QColor(SEL_BG))
    palette.setColor(role.ToolTipText, QColor(SEL_FG))
    palette.setColor(role.Text, QColor(FG))
    palette.setColor(role.Button, QColor(BG_PANEL))
    palette.setColor(role.ButtonText, QColor(FG))
    palette.setColor(role.BrightText, QColor(FG_STRONG))
    palette.setColor(role.Highlight, QColor(SEL_BG))
    palette.setColor(role.HighlightedText, QColor(SEL_FG))
    palette.setColor(role.PlaceholderText, QColor(FG_DIM))
    palette.setColor(role.Link, QColor(FG_STRONG))
    for disabled in (role.Text, role.ButtonText, role.WindowText):
        palette.setColor(group.Disabled, disabled, QColor(DISABLED_FG))

    app.setPalette(palette)
    app.setStyleSheet(MONO_QSS)
