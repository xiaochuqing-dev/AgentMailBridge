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
FONT_FAMILY = "Microsoft YaHei UI"

# Qt 样式表不支持 CSS line-height；行高由控件高度与布局间距落实。
TYPOGRAPHY = {
    "app_title": {"size": 18, "weight": 700, "line_height": 26},
    "page_title": {"size": 18, "weight": 700, "line_height": 27},
    "section_title": {"size": 15, "weight": 700, "line_height": 23},
    "card_title": {"size": 13, "weight": 700, "line_height": 21},
    "body": {"size": 12, "weight": 400, "line_height": 20},
    "secondary_body": {"size": 11, "weight": 400, "line_height": 18},
    "caption": {"size": 10, "weight": 400, "line_height": 16},
    "button": {"size": 12, "weight": 400, "line_height": 20},
    "table_header": {"size": 11, "weight": 700, "line_height": 18},
    "table_cell": {"size": 11, "weight": 400, "line_height": 18},
    "status": {"size": 10, "weight": 700, "line_height": 16},
}
_INTERFACE_FONT_IDS: list[int] = []


def load_interface_font() -> QFont:
    """选择 Windows 原生清晰字体，避免重复加载造成字体回退不稳定。"""
    if not _INTERFACE_FONT_IDS:
        for path in (
            Path("C:/Windows/Fonts/msyh.ttc"),
            Path("C:/Windows/Fonts/msyhbd.ttc"),
        ):
            if path.exists():
                font_id = QFontDatabase.addApplicationFont(str(path))
                if font_id >= 0:
                    _INTERFACE_FONT_IDS.append(font_id)
    installed = set(QFontDatabase.families())
    family = FONT_FAMILY if FONT_FAMILY in installed else "Segoe UI"
    font = QFont(family, 10, QFont.Weight.Normal)
    font.setStyleStrategy(
        QFont.StyleStrategy.PreferAntialias | QFont.StyleStrategy.PreferQuality
    )
    font.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
    return font


def build_stylesheet(theme: str = "light") -> str:
    """生成指定的全局主题样式。"""
    base = f"""
    * {{
        font-family: "Microsoft YaHei UI";
        font-size: 12px;
        font-weight: 400;
        color: {TEXT};
        outline: none;
    }}
    QMainWindow, QWidget#windowRoot {{
        background: #FFFFFF;
    }}
    QWidget#titleBar, QWidget#sidebar, QWidget#rightPanelContent,
    QWidget#centralPanel, QWidget#pageSurface, QWidget#bodySurface,
    QWidget#tabBar, QScrollArea#rightPanel, QScrollArea#pageScroll {{
        background: #FFFFFF;
    }}
    QScrollArea#rightPanel, QWidget#rightPanelContent {{
        background: #FCFCFE;
        border: none;
    }}
    QWidget#sidebar {{
        border: none;
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
        font-size: 15px;
        font-weight: 700;
    }}
    QLabel#pageTitle {{
        font-size: 18px;
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
        font-size: 12px;
    }}
    QLabel#oauthExpectedAccount {{
        color: #4A3E87;
        background: #F4F1FF;
        border: 1px solid #D8D0FF;
        border-radius: 4px;
        padding: 7px 9px;
        font-size: 11px;
    }}
    QLabel#oauthResultDetail {{
        border: 1px solid transparent;
        border-radius: 4px;
        padding: 8px 10px;
        font-size: 11px;
        font-weight: 600;
    }}
    QLabel#oauthResultDetail[severity="warning"] {{
        color: #8A5700;
        background: #FFF7E8;
        border-color: #F2D39A;
    }}
    QLabel#oauthResultDetail[severity="danger"] {{
        color: {DANGER};
        background: #FFF2F4;
        border-color: #F0BCC2;
    }}
    QLabel#sendFileValue {{ font-size: 12px; font-weight: 700; }}
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
    QLabel#statusValue[statusState="success"] {{ color: {SUCCESS}; font-weight: 700; }}
    QLabel#statusValue[statusState="danger"] {{ color: {DANGER}; font-weight: 700; }}
    QLabel#tag {{
        color: {SUCCESS};
        background: {SUCCESS_SOFT};
        border-radius: 9px;
        padding: 2px 7px;
        font-size: 9px;
        font-weight: 700;
    }}
    QLabel#tag[configured="false"] {{
        color: {TEXT_MUTED};
        background: #F1F2F5;
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
    QFrame#navCard {{
        background: #FFFFFF;
        border: 1px solid {BORDER};
        border-radius: 10px;
    }}
    QFrame#accountPanel, QFrame#credentialCard {{
        background: #FBFAFF;
        border: 1px solid #E4DFFF;
        border-radius: 10px;
    }}
    QLabel#credentialMask {{
        color: #3E4350;
        background: #F0F1F5;
        border: 1px solid #E0E2E8;
        border-radius: 5px;
        padding: 7px 11px;
        font-family: "Segoe UI";
        letter-spacing: 2px;
    }}
    QFrame#heroCard {{
        background: #FFFFFF;
        border: 1px solid {BORDER};
        border-radius: 10px;
    }}
    QFrame#overviewMetric {{
        background: #F8F9FC;
        border: 1px solid #ECEEF4;
        border-radius: 7px;
    }}
    QLabel#overviewValue {{
        color: #343744;
        font-size: 13px;
        font-weight: 700;
    }}
    QFrame#healthItem {{
        background: #FBFBFE;
        border: 1px solid #ECEEF4;
        border-radius: 8px;
    }}
    QLabel#healthName {{ font-size: 11px; font-weight: 700; color: #343744; }}
    QLabel#healthState {{ font-size: 10px; font-weight: 700; color: {TEXT_MUTED}; }}
    QLabel#healthState[healthState="normal"] {{ color: {SUCCESS}; }}
    QLabel#healthState[healthState="partial"] {{ color: #A76500; }}
    QLabel#healthState[healthState="fault"] {{ color: {DANGER}; }}
    QLabel#healthDetail {{ font-size: 10px; color: #565C6B; }}
    QLabel#healthChecked {{ font-size: 9px; color: {TEXT_MUTED}; }}
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
        font-size: 12px;
    }}
    QPushButton:hover {{
        border-color: #BDB4F8;
        background: #FAF9FF;
    }}
    QPushButton:pressed {{ background: {PURPLE_SOFT}; }}
    QPushButton:focus {{ border: 1px solid {PURPLE}; }}
    QPushButton:disabled {{
        color: #AFB2BC;
        background: #F4F5F7;
        border-color: #E6E8ED;
    }}
    QPushButton[taskState="running"] {{
        color: #FFFFFF;
        border-color: #6750E8;
        background: #6750E8;
        font-weight: 700;
    }}
    QPushButton[taskState="success"] {{
        color: #FFFFFF;
        border-color: {SUCCESS};
        background: {SUCCESS};
    }}
    QPushButton[taskState="error"] {{
        color: #FFFFFF;
        border-color: {DANGER};
        background: {DANGER};
    }}
    QPushButton[taskState="warning"] {{
        color: #FFFFFF;
        border-color: {WARNING};
        background: {WARNING};
    }}
    QPushButton#primaryButton {{
        color: #FFFFFF;
        font-weight: 700;
        border: none;
        background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 {PURPLE}, stop:1 #2E86DE);
    }}
    QPushButton#primaryButton:hover {{
        background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 {PURPLE_DARK}, stop:1 #2478C8);
    }}
    QPushButton#primaryButton:pressed {{ background: #4930D9; }}
    QPushButton#outlinePurple {{
        color: {PURPLE};
        font-weight: 700;
        border: 1px solid #8F7CF2;
        background: #FFFFFF;
    }}
    QPushButton#accountChoice {{
        text-align: left;
        color: #353945;
        border: 1px solid #DED9F8;
        border-radius: 10px;
        background: #FFFFFF;
        padding: 12px 16px;
        font-size: 12px;
        font-weight: 700;
    }}
    QPushButton#accountChoice:hover {{
        color: {PURPLE};
        border-color: #9E8EF5;
        background: #FAF9FF;
    }}
    QPushButton#accountChoice:checked {{
        color: {PURPLE};
        border: 2px solid {PURPLE};
        background: {PURPLE_SOFT};
    }}
    QPushButton#textButton {{
        color: {PURPLE};
        font-weight: 700;
        border: 1px solid #E2DEFA;
        border-radius: 6px;
        background: #F9F8FF;
        padding: 5px 9px;
    }}
    QPushButton#textButton:hover {{ border-color: #B8ADF5; background: {PURPLE_SOFT}; }}
    QPushButton#compactButton {{
        min-height: 26px;
        color: #4A3CB3;
        background: #F5F3FF;
        border: 1px solid #DDD7FA;
        border-radius: 5px;
        padding: 3px 8px;
        font-size: 10px;
        font-weight: 700;
    }}
    QPushButton#compactButton:hover {{ background: #EDE9FF; border-color: #A99AF2; }}
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
        padding: 11px 14px;
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
    QFrame#navCard QPushButton#navButton {{ border-radius: 0; }}
    QPushButton#tabButton {{
        border: none;
        border-bottom: 2px solid transparent;
        border-radius: 0;
        background: transparent;
        padding: 15px 18px 12px 18px;
        font-size: 12px;
        font-weight: 700;
    }}
    QPushButton#tabButton:hover {{ color: {PURPLE}; background: transparent; }}
    QPushButton#tabButton:checked {{
        color: {PURPLE};
        border-bottom: 2px solid {PURPLE};
        font-weight: 700;
    }}
    QLineEdit, QComboBox, QSpinBox, QTextEdit {{
        min-height: 35px;
        background: #FFFFFF;
        border: 1px solid #E1E3E9;
        border-radius: 5px;
        padding: 0 10px;
        font-size: 12px;
        selection-background-color: {PURPLE};
    }}
    QTextEdit {{ padding: 8px; }}
    QLineEdit:focus, QComboBox:focus, QSpinBox:focus {{
        border: 1px solid #8E7AF4;
    }}
    QLineEdit#inboxSearch {{
        min-height: 36px;
        padding-left: 8px;
        border-radius: 7px;
        background: #FBFBFE;
    }}
    QComboBox::drop-down {{ border: none; width: 26px; }}
    QComboBox QAbstractItemView {{
        background: #FFFFFF;
        border: 1px solid {BORDER};
        selection-background-color: {PURPLE_SOFT};
        selection-color: {PURPLE};
        padding: 4px;
    }}
    QCheckBox {{ spacing: 8px; font-size: 12px; }}
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
        border: 1px solid {BORDER};
        border-radius: 8px;
        gridline-color: #EEF0F4;
        font-size: 11px;
        selection-background-color: {PURPLE_SOFT};
        selection-color: {TEXT};
    }}
    QTableWidget::item {{ padding: 5px 6px; border-bottom: 1px solid #F0F1F4; }}
    QTableWidget::item:hover {{ background: transparent; }}
    QTableWidget#mailRecordTable {{
        alternate-background-color: #FFFFFF;
        selection-background-color: transparent;
        selection-color: {TEXT};
    }}
    QTableWidget#mailRecordTable::item {{
        background: transparent;
        border: none;
        border-bottom: 1px solid #ECEEF3;
        color: {TEXT};
    }}
    QTableWidget#mailRecordTable::item:hover,
    QTableWidget#mailRecordTable::item:selected,
    QTableWidget#mailRecordTable::item:focus {{
        background: transparent;
        border: none;
        border-bottom: 1px solid #ECEEF3;
        color: {TEXT};
    }}
    QTableWidget#compactResourceTable {{
        alternate-background-color: #FFFFFF;
        selection-background-color: transparent;
        selection-color: {TEXT};
    }}
    QTableWidget#compactResourceTable::item,
    QTableWidget#compactResourceTable::item:hover,
    QTableWidget#compactResourceTable::item:selected,
    QTableWidget#compactResourceTable::item:focus {{
        background: transparent;
        border: none;
        border-bottom: 1px solid #ECEEF3;
        color: {TEXT};
    }}
    QHeaderView::section {{
        color: #777C8B;
        background: #F4F5F8;
        border: none;
        border-bottom: 1px solid {BORDER};
        padding: 6px;
        font-size: 11px;
        font-weight: 700;
        text-align: left;
    }}
    QScrollBar:vertical {{ width: 7px; background: transparent; margin: 1px; }}
    QScrollBar::handle:vertical {{ background: #D7D9E1; border-radius: 3px; min-height: 28px; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
    QScrollBar:horizontal {{ height: 9px; background: #F5F6F9; margin: 1px; }}
    QScrollBar::handle:horizontal {{ background: #C9CCD6; border-radius: 4px; min-width: 32px; }}
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
    QWidget#verticalResizeHandle {{ background: transparent; }}
    QWidget#verticalResizeHandle:hover {{ background: {PURPLE}; }}
    QScrollArea {{ border: none; background: #FFFFFF; }}
    QProgressBar {{
        border: none;
        background: #EEEAFD;
        border-radius: 2px;
    }}
    QProgressBar::chunk {{
        border-radius: 2px;
        background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 {PURPLE}, stop:1 #2EA7E0);
    }}
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
    QMainWindow, QDialog, QWidget#windowRoot, QWidget#titleBar, QWidget#sidebar,
    QWidget#centralPanel, QWidget#rightPanelContent, QWidget#pageSurface,
    QWidget#bodySurface, QWidget#tabBar, QScrollArea {{ background: #171923; }}
    QScrollArea#rightPanel, QWidget#rightPanelContent {{ background: #1C1E2A; border: none; }}
    QWidget#sidebar, QWidget#titleBar, QWidget#tabBar {{ border-color: #343746; }}
    QLabel#fieldLabel, QLabel#muted, QLabel#hint {{ color: #AEB4C5; }}
    QLabel#oauthExpectedAccount {{
        color: #D6CEFF;
        background: #29243C;
        border-color: #5A4F82;
    }}
    QLabel#oauthResultDetail[severity="warning"] {{
        color: #FFD27A;
        background: #352B1C;
        border-color: #6A5329;
    }}
    QLabel#oauthResultDetail[severity="danger"] {{
        color: #FF9CA5;
        background: #382128;
        border-color: #713743;
    }}
    QLabel#statusName, QLabel#statusValue, QLabel#tipText, QLabel#statNumber,
    QLabel#statCaption, QLabel#healthName, QLabel#healthDetail {{ color: #C7CBD8; }}
    QFrame#card, QLineEdit, QComboBox, QSpinBox, QTextEdit, QTableWidget,
    QHeaderView::section, QComboBox QAbstractItemView {{
        background: #222532;
        border-color: #3A3E50;
    }}
    QFrame#heroCard, QFrame#overviewMetric, QFrame#statPurple, QFrame#statGreen,
    QFrame#statBlue, QFrame#statRed, QFrame#healthItem {{
        background: #242736;
        border-color: #3A3E50;
    }}
    QFrame#accountPanel, QFrame#credentialCard {{
        background: #222532;
        border-color: #3A3E50;
    }}
    QLabel#credentialMask {{
        color: #D5D8E3;
        background: #1D202C;
        border-color: #3A3E50;
    }}
    QLineEdit#inboxSearch {{ background: #222532; border-color: #3A3E50; }}
    QLabel#overviewValue {{ color: #E8EAF2; }}
    QFrame#navCard {{ background: #222532; border-color: #3A3E50; }}
    QFrame#separator {{ background: #343746; }}
    QTableWidget {{
        alternate-background-color: #1D202C;
        gridline-color: #343746;
        selection-background-color: #30354B;
        selection-color: #E8EAF2;
    }}
    QTableWidget::item {{ border-bottom-color: #343746; }}
    QTableWidget::item:selected {{ background: #30354B; color: #E8EAF2; }}
    QTableWidget::item:hover {{ background: transparent; color: #E8EAF2; }}
    QTableWidget#mailRecordTable {{
        alternate-background-color: #222532;
        selection-background-color: transparent;
        selection-color: #E8EAF2;
    }}
    QTableWidget#mailRecordTable::item,
    QTableWidget#mailRecordTable::item:hover,
    QTableWidget#mailRecordTable::item:selected,
    QTableWidget#mailRecordTable::item:focus {{
        background: transparent;
        border: none;
        border-bottom: 1px solid #343746;
        color: #E8EAF2;
    }}
    QTableWidget#compactResourceTable {{
        alternate-background-color: #222532;
        selection-background-color: transparent;
        selection-color: #E8EAF2;
    }}
    QTableWidget#compactResourceTable::item,
    QTableWidget#compactResourceTable::item:hover,
    QTableWidget#compactResourceTable::item:selected,
    QTableWidget#compactResourceTable::item:focus {{
        background: transparent;
        border: none;
        border-bottom: 1px solid #343746;
        color: #E8EAF2;
    }}
    QHeaderView::section {{ color: #BFC4D3; }}
    QPushButton {{ background: #292C3A; border-color: #42465A; }}
    QPushButton:hover, QPushButton#titleButton:hover {{ background: #35394B; }}
    QPushButton#outlinePurple {{ color: #C6BEFF; background: #242736; }}
    QPushButton#textButton, QPushButton#compactButton {{
        color: #C6BEFF;
        background: #292C3A;
        border-color: #4A4E63;
    }}
    QPushButton#accountChoice {{
        color: #D5D8E3;
        background: #222532;
        border-color: #4A4E63;
    }}
    QPushButton#accountChoice:hover {{
        color: #C6BEFF;
        background: #292C3A;
        border-color: #8072D9;
    }}
    QPushButton#accountChoice:checked {{
        color: #C6BEFF;
        background: #30304B;
        border-color: {PURPLE};
    }}
    QPushButton#tabButton, QPushButton#navButton {{ background: transparent; }}
    QPushButton#navButton, QPushButton#tabButton {{ color: #C7CBD8; }}
    QScrollBar::handle:vertical {{ background: #555A6C; }}
    """
