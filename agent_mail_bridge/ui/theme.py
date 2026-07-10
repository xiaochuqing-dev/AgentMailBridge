"""正式界面的颜色、字号与 Qt 样式。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QFont, QFontDatabase

PURPLE = "#5B3DF5"
PURPLE_DARK = "#4930D9"
PURPLE_SOFT = "#F1EEFF"
TEXT = "#20212A"
TEXT_MUTED = "#727787"
BORDER = "#E8EAF0"
BACKGROUND = "#F7F8FB"
SUCCESS = "#18A957"
SUCCESS_SOFT = "#ECFAF1"
WARNING = "#E59A24"
DANGER = "#D84A56"


def load_interface_font() -> QFont:
    """显式加载中文字体，避免 Qt 离屏或精简环境显示方框。"""
    candidates = (
        (Path("C:/Windows/Fonts/NotoSansSC-VF.ttf"), "Noto Sans SC"),
        (Path("C:/Windows/Fonts/msyh.ttc"), "Microsoft YaHei UI"),
    )
    for path, family in candidates:
        if path.exists() and QFontDatabase.addApplicationFont(str(path)) >= 0:
            return QFont(family, 10)
    return QFont("Microsoft YaHei UI", 10)


def build_stylesheet(theme: str = "light") -> str:
    """生成指定的全局主题样式。"""
    base = f"""
    * {{
        font-family: "Noto Sans SC", "Microsoft YaHei UI", "Segoe UI";
        color: {TEXT};
        outline: none;
    }}
    QMainWindow, QWidget#windowRoot {{
        background: #FFFFFF;
    }}
    QWidget#titleBar, QWidget#sidebar, QWidget#rightPanel,
    QWidget#centralPanel, QWidget#pageSurface, QWidget#bodySurface,
    QWidget#tabBar {{
        background: #FFFFFF;
    }}
    QWidget#rightPanel {{
        background: #FCFCFE;
        border-left: 1px solid {BORDER};
    }}
    QWidget#sidebar {{
        border-right: 1px solid {BORDER};
    }}
    QWidget#titleBar {{
        border-bottom: 1px solid {BORDER};
    }}
    QWidget#tabBar {{
        border-bottom: 1px solid {BORDER};
    }}
    QLabel {{
        background: transparent;
    }}
    QLabel#appTitle {{
        font-size: 18px;
        font-weight: 700;
    }}
    QLabel#version {{
        color: {PURPLE};
        font-size: 10px;
        font-weight: 700;
    }}
    QLabel#sectionTitle {{
        font-size: 16px;
        font-weight: 700;
    }}
    QLabel#pageTitle {{
        font-size: 17px;
        font-weight: 700;
    }}
    QLabel#minorTitle {{
        font-size: 13px;
        font-weight: 700;
    }}
    QLabel#muted, QLabel#hint {{
        color: {TEXT_MUTED};
        font-size: 11px;
    }}
    QLabel#fieldLabel {{
        color: #555A68;
        font-size: 11px;
    }}
    QLabel#successText {{ color: {SUCCESS}; font-weight: 700; }}
    QLabel#errorText {{ color: {DANGER}; }}
    QLabel#purpleText {{ color: {PURPLE}; font-weight: 700; }}
    QLabel#statusPill {{
        color: {SUCCESS};
        background: {SUCCESS_SOFT};
        border-radius: 15px;
        padding: 5px 14px;
        font-size: 11px;
        font-weight: 700;
    }}
    QLabel#tag {{
        color: {SUCCESS};
        background: {SUCCESS_SOFT};
        border-radius: 9px;
        padding: 2px 7px;
        font-size: 9px;
        font-weight: 700;
    }}
    QLabel#iconBadge {{
        color: {PURPLE};
        background: {PURPLE_SOFT};
        border-radius: 8px;
        font-size: 16px;
        font-weight: 700;
    }}
    QFrame#separator {{
        background: {BORDER};
        border: none;
        max-height: 1px;
        min-height: 1px;
    }}
    QFrame#card {{
        background: #FFFFFF;
        border: 1px solid {BORDER};
        border-radius: 10px;
    }}
    QFrame#statPurple {{
        background: #F5F2FF;
        border: 1px solid #EEE9FF;
        border-radius: 9px;
    }}
    QFrame#statGreen {{
        background: #F0FAF4;
        border: 1px solid #E4F4EA;
        border-radius: 9px;
    }}
    QFrame#statBlue {{
        background: #EFF8FD;
        border: 1px solid #E3F1F9;
        border-radius: 9px;
    }}
    QFrame#statRed {{
        background: #FFF4F4;
        border: 1px solid #F9E8EA;
        border-radius: 9px;
    }}
    QPushButton {{
        background: #FFFFFF;
        border: 1px solid #DDE0E8;
        border-radius: 6px;
        padding: 7px 13px;
        font-size: 11px;
    }}
    QPushButton:hover {{
        border-color: #BDB4F8;
        background: #FAF9FF;
    }}
    QPushButton:pressed {{ background: {PURPLE_SOFT}; }}
    QPushButton:disabled {{
        color: #AFB2BC;
        background: #F4F5F7;
        border-color: #E6E8ED;
    }}
    QPushButton#primaryButton {{
        color: #FFFFFF;
        font-weight: 700;
        border: none;
        background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 {PURPLE}, stop:1 #7247F6);
    }}
    QPushButton#primaryButton:hover {{ background: {PURPLE_DARK}; }}
    QPushButton#outlinePurple {{
        color: {PURPLE};
        font-weight: 700;
        border: 1px solid #8F7CF2;
        background: #FFFFFF;
    }}
    QPushButton#textButton {{
        color: {PURPLE};
        font-weight: 700;
        border: none;
        background: transparent;
        padding: 4px;
    }}
    QPushButton#titleButton {{
        border: none;
        border-radius: 4px;
        background: transparent;
        padding: 0;
        font-size: 15px;
    }}
    QPushButton#titleButton:hover {{ background: #F0F1F5; }}
    QPushButton#closeButton {{
        border: none;
        border-radius: 4px;
        background: transparent;
        padding: 0;
        font-size: 15px;
    }}
    QPushButton#closeButton:hover {{ color: #FFFFFF; background: #E84D4D; }}
    QPushButton#navButton {{
        text-align: left;
        border: none;
        border-radius: 7px;
        padding: 9px 12px;
        background: transparent;
        color: #4F5360;
        font-size: 12px;
    }}
    QPushButton#navButton:hover {{ background: #F5F3FF; color: {PURPLE}; }}
    QPushButton#navButton:checked {{
        background: {PURPLE_SOFT};
        color: {PURPLE};
        font-weight: 700;
    }}
    QPushButton#tabButton {{
        border: none;
        border-bottom: 2px solid transparent;
        border-radius: 0;
        background: transparent;
        padding: 15px 18px 12px 18px;
        font-size: 12px;
        font-weight: 600;
    }}
    QPushButton#tabButton:hover {{ color: {PURPLE}; background: transparent; }}
    QPushButton#tabButton:checked {{
        color: {PURPLE};
        border-bottom: 2px solid {PURPLE};
        font-weight: 700;
    }}
    QLineEdit, QComboBox, QSpinBox {{
        min-height: 33px;
        background: #FFFFFF;
        border: 1px solid #E1E3E9;
        border-radius: 5px;
        padding: 0 10px;
        font-size: 11px;
        selection-background-color: {PURPLE};
    }}
    QLineEdit:focus, QComboBox:focus, QSpinBox:focus {{
        border: 1px solid #8E7AF4;
    }}
    QComboBox::drop-down {{ border: none; width: 26px; }}
    QComboBox QAbstractItemView {{
        background: #FFFFFF;
        border: 1px solid {BORDER};
        selection-background-color: {PURPLE_SOFT};
        selection-color: {PURPLE};
        padding: 4px;
    }}
    QCheckBox {{ spacing: 8px; font-size: 11px; }}
    QCheckBox::indicator {{ width: 15px; height: 15px; }}
    QCheckBox::indicator:unchecked {{
        border: 1px solid #C7CAD3;
        border-radius: 3px;
        background: #FFFFFF;
    }}
    QCheckBox::indicator:checked {{
        border: 1px solid {SUCCESS};
        border-radius: 3px;
        background: {SUCCESS};
    }}
    QTableWidget {{
        background: #FFFFFF;
        alternate-background-color: #FBFBFD;
        border: none;
        gridline-color: #EEF0F4;
        font-size: 10px;
        selection-background-color: {PURPLE_SOFT};
        selection-color: {TEXT};
    }}
    QTableWidget::item {{ padding: 5px 6px; border-bottom: 1px solid #F0F1F4; }}
    QHeaderView::section {{
        color: #777C8B;
        background: #FFFFFF;
        border: none;
        border-bottom: 1px solid {BORDER};
        padding: 6px;
        font-size: 10px;
        font-weight: 600;
        text-align: left;
    }}
    QScrollBar:vertical {{ width: 7px; background: transparent; margin: 1px; }}
    QScrollBar::handle:vertical {{ background: #D7D9E1; border-radius: 3px; min-height: 28px; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
    QScrollArea {{ border: none; background: #FFFFFF; }}
    QToolTip {{
        color: #FFFFFF;
        background: #2F3040;
        border: none;
        padding: 5px 8px;
        font-size: 10px;
    }}
    """
    if theme != "dark":
        return base
    return base + f"""
    * {{ color: #E8EAF2; }}
    QMainWindow, QWidget#windowRoot, QWidget#titleBar, QWidget#sidebar,
    QWidget#centralPanel, QWidget#rightPanel, QWidget#pageSurface,
    QWidget#bodySurface, QWidget#tabBar, QScrollArea {{ background: #171923; }}
    QWidget#rightPanel {{ background: #1C1E2A; border-left-color: #343746; }}
    QWidget#sidebar, QWidget#titleBar, QWidget#tabBar {{ border-color: #343746; }}
    QLabel#fieldLabel, QLabel#muted, QLabel#hint {{ color: #AEB4C5; }}
    QFrame#card, QLineEdit, QComboBox, QSpinBox, QTableWidget,
    QHeaderView::section, QComboBox QAbstractItemView {{
        background: #222532;
        border-color: #3A3E50;
    }}
    QTableWidget {{ alternate-background-color: #1D202C; gridline-color: #343746; }}
    QHeaderView::section {{ color: #BFC4D3; }}
    QPushButton {{ background: #292C3A; border-color: #42465A; }}
    QPushButton:hover, QPushButton#titleButton:hover {{ background: #35394B; }}
    QPushButton#outlinePurple {{ background: #242736; }}
    QPushButton#textButton, QPushButton#tabButton, QPushButton#navButton {{ background: transparent; }}
    QScrollBar::handle:vertical {{ background: #555A6C; }}
    """
