"""ROLAVI look of the window (applied to the window only, never to QGIS itself).

Same language as ROLAVI Viewer: navy title bar, white cards on a light grey workspace,
ROLAVI blue for the main action, dark plan view.
"""

ROLAVI_BLUE = "#0878C9"
ROLAVI_BLUE_DARK = "#0866AD"
NAVY = "#07131F"
DARK_BG = NAVY
WORKSPACE = "#F3F6F9"
BORDER = "#DCE4EC"
FIELD_BORDER = "#C9D5E0"
TEXT_DARK = "#172531"
TEXT_MUTED = "#5B6B7A"
GOOD = "#1B7F3A"
BAD = "#C62828"
PICK = "#FF9900"

FONT = '"Segoe UI", "Inter", "Helvetica Neue", Arial, sans-serif'
MONO = 'Consolas, "JetBrains Mono", "Courier New", monospace'

STYLE = f"""
QWidget#RolaviRoot {{ background: {WORKSPACE}; color: {TEXT_DARK}; font-family: {FONT}; font-size: 10pt; }}

/* ---------- title bar ---------- */
QFrame#TitleBar {{ background: {NAVY}; border: none; }}
QFrame#TitleSep {{ background: #26405A; border: none; }}
QLabel#Product {{ color: #FFFFFF; font-size: 11.5pt; font-weight: 600; }}
QLabel#VersionPill {{ color: #A9C0D3; background: #13283A; border-radius: 9px; padding: 1px 9px; font-size: 8pt; }}
QLabel#TitleMuted {{ color: #A9C0D3; font-size: 9pt; }}
QPushButton#AccountChip {{ color: #FFFFFF; background: #0E2133; border: 1px solid #26405A; border-radius: 16px;
    padding: 5px 14px; font-weight: 600; min-height: 20px; }}
QPushButton#AccountChip:hover {{ border-color: {ROLAVI_BLUE}; color: #FFFFFF; background: #0E2133; }}

/* ---------- workflow rail ---------- */
QFrame#StepRail {{ background: #FFFFFF; border: none; border-right: 1px solid {BORDER}; }}
QLabel#RailHeader {{ color: {TEXT_MUTED}; font-size: 8pt; font-weight: 600; padding: 0 10px 4px 10px; }}
QFrame#StepItem {{ background: transparent; border: none; border-left: 3px solid transparent; border-radius: 8px; }}
QFrame#StepItem:hover {{ background: {WORKSPACE}; }}
QFrame#StepItem[state="active"] {{ background: #E8F2FB; border-left: 3px solid {ROLAVI_BLUE}; }}
QFrame#StepItem:focus {{ border-left: 3px solid #7FB2DD; }}
QLabel#StepBadge {{ border-radius: 14px; border: 1px solid #B8C6D3; background: #FFFFFF; color: {TEXT_MUTED};
    font-weight: 700; font-size: 9pt; }}
QLabel#StepBadge[state="active"] {{ background: {ROLAVI_BLUE}; border-color: {ROLAVI_BLUE}; color: #FFFFFF; }}
QLabel#StepBadge[state="done"] {{ background: {GOOD}; border-color: {GOOD}; color: #FFFFFF; }}
QLabel#StepTitle {{ color: {TEXT_DARK}; font-weight: 600; }}
QLabel#StepTitle[state="active"] {{ color: {ROLAVI_BLUE_DARK}; }}
QLabel#StepSub {{ color: {TEXT_MUTED}; font-size: 9pt; }}
QLabel#RailFoot {{ color: {TEXT_MUTED}; font-size: 8pt; }}
QListWidget#LayerList {{ background: #FFFFFF; border: 1px solid {BORDER}; border-radius: 8px; padding: 3px;
    color: {TEXT_DARK}; font-size: 9pt; outline: none; }}
QListWidget#LayerList::item {{ padding: 5px 4px; border-radius: 6px; border: 1px solid transparent; }}
QListWidget#LayerList::item:hover {{ background: {WORKSPACE}; }}
QListWidget#LayerList::item:selected {{ background: #E8F2FB; color: {TEXT_DARK}; border: 1px solid {ROLAVI_BLUE}; }}
QPushButton#LayerAdd {{ background: transparent; color: {ROLAVI_BLUE}; border: none; font-weight: 700;
    font-size: 9pt; padding: 0 8px 4px 8px; }}
QPushButton#LayerAdd:hover {{ color: {ROLAVI_BLUE_DARK}; }}
QPushButton#LayerAdd:disabled {{ color: #A9B6C2; }}

/* ---------- pages and cards ---------- */
QStackedWidget#Pages, QScrollArea#PageScroll, QWidget#PageInner {{ background: {WORKSPACE}; border: none; }}
QLabel#Eyebrow {{ color: {ROLAVI_BLUE_DARK}; font-size: 8pt; font-weight: 600; }}
QLabel#PageTitle {{ color: #0E1D2A; font-size: 16pt; font-weight: 700; }}
QLabel#PageDesc {{ color: #4A5A69; }}
QFrame#PageFooter {{ background: {WORKSPACE}; border: none; border-top: 1px solid {BORDER}; }}
QFrame#Card {{ background: #FFFFFF; border: 1px solid {BORDER}; border-radius: 12px; }}
QLabel#CardTitle {{ color: #0E1D2A; font-weight: 700; }}
QLabel#FieldLabel {{ color: #3A4B5A; font-weight: 600; font-size: 9pt; }}
QLabel#Fact {{ background: {WORKSPACE}; border-radius: 8px; padding: 6px 10px; color: #0E1D2A; }}
QLabel#Muted {{ color: {TEXT_MUTED}; font-weight: normal; }}
QLabel#Info {{ color: {TEXT_DARK}; font-weight: normal; }}
QLabel#Bad {{ color: {BAD}; font-weight: normal; }}
QLabel#Good {{ color: {GOOD}; font-weight: normal; }}
QLabel#AxisTag {{ color: {TEXT_MUTED}; font-weight: 600; font-size: 9pt; }}

/* ---------- fields ---------- */
QLineEdit, QComboBox, QSpinBox {{ background: #FFFFFF; color: {TEXT_DARK}; border: 1px solid {FIELD_BORDER};
    border-radius: 8px; padding: 6px 8px; min-height: 20px; selection-background-color: {ROLAVI_BLUE}; }}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus {{ border: 1px solid {ROLAVI_BLUE}; }}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled {{ background: #F1F4F7; color: #8A99A8; }}
QLineEdit[readOnly="true"] {{ background: #F7F9FB; }}
QComboBox::drop-down {{ border: none; width: 26px; }}
QComboBox QAbstractItemView {{ background: #FFFFFF; border: 1px solid {FIELD_BORDER}; selection-background-color: #E8F2FB;
    selection-color: {TEXT_DARK}; outline: none; }}
QCheckBox {{ spacing: 8px; color: {TEXT_DARK}; }}
QCheckBox:disabled {{ color: #8A99A8; }}

/* ---------- buttons ---------- */
QPushButton {{ background: #FFFFFF; color: {TEXT_DARK}; border: 1px solid {FIELD_BORDER}; border-radius: 8px;
    padding: 7px 14px; font-weight: 600; min-height: 20px; }}
QPushButton:hover {{ border-color: {ROLAVI_BLUE}; color: {ROLAVI_BLUE_DARK}; }}
QPushButton:pressed {{ background: #E8F2FB; }}
QPushButton:disabled {{ color: #A0AEBB; background: #F1F4F7; border-color: {BORDER}; }}
QPushButton:checked {{ background: #FFF1DC; border-color: #E08A00; color: #8A4B00; }}
QPushButton#Primary {{ background: {ROLAVI_BLUE}; color: #FFFFFF; border: none; padding: 9px 18px; font-size: 10.5pt; }}
QPushButton#Primary:hover {{ background: #066AB0; color: #FFFFFF; }}
QPushButton#Primary:pressed {{ background: #05589A; }}
QPushButton#Primary:disabled {{ background: #9CC4E4; color: #EEF5FB; }}
QPushButton#Ghost {{ background: transparent; border: none; color: #3A4B5A; }}
QPushButton#Ghost:hover {{ color: {ROLAVI_BLUE_DARK}; }}
QFrame#Segmented {{ background: #EEF2F6; border: none; border-radius: 10px; }}
QPushButton#SegmentButton {{ background: transparent; border: 1px solid transparent; border-radius: 8px; color: #3A4B5A;
    font-weight: 500; padding: 7px 10px; }}
QPushButton#SegmentButton:hover {{ color: {ROLAVI_BLUE_DARK}; border-color: transparent; }}
QPushButton#SegmentButton:checked {{ background: #FFFFFF; color: {ROLAVI_BLUE_DARK}; font-weight: 600; border: 1px solid #D5DFE8; }}
QPushButton#SegmentButton:disabled {{ color: #A0AEBB; }}

/* ---------- GCP table ---------- */
QTableWidget {{ background: #FFFFFF; border: 1px solid #E1E8EF; border-radius: 8px; gridline-color: #EDF1F5;
    selection-background-color: #FFF1DC; selection-color: {TEXT_DARK}; }}
QHeaderView::section {{ background: {WORKSPACE}; color: {TEXT_MUTED}; border: none; border-bottom: 1px solid #E1E8EF;
    padding: 5px 6px; font-weight: 600; font-size: 9pt; }}

/* ---------- plan view and log ---------- */
QFrame#PlanBar {{ background: {NAVY}; border: none; border-bottom: 1px solid #1C3346; }}
QLabel#PlanCaption {{ color: #D6E2EC; font-size: 9pt; }}
QPushButton#DarkTool {{ background: #16314A; color: #FFFFFF; border: 1px solid #26405A; border-radius: 8px; padding: 5px 12px; }}
QPushButton#DarkTool:hover {{ border-color: {ROLAVI_BLUE}; color: #FFFFFF; }}
QPushButton#DarkTool:disabled {{ color: #5F7486; background: #0E2133; border-color: #1C3346; }}
QPushButton#DarkTool:checked {{ background: {ROLAVI_BLUE}; border-color: {ROLAVI_BLUE}; color: #FFFFFF; }}
QWidget#View3DHolder {{ background: {NAVY}; }}
QLabel#ViewNote {{ color: #D6E2EC; font-size: 10pt; padding: 24px; }}
QFrame#LogFrame {{ background: {NAVY}; border: none; border-top: 1px solid #1C3346; }}
QLabel#LogTitle {{ color: #FFFFFF; font-weight: 600; font-size: 9pt; padding: 6px 2px; border-bottom: 2px solid {ROLAVI_BLUE}; }}
QPlainTextEdit#Console {{ background: {NAVY}; color: #C9D6E2; border: none; font-family: {MONO}; font-size: 9pt; }}

/* ---------- splitters and scroll bars ---------- */
QSplitter::handle {{ background: {BORDER}; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #C9D5E0; border-radius: 3px; min-height: 30px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: #C9D5E0; border-radius: 3px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
"""

STATUS_STYLE = f"""
QStatusBar {{ background: #FFFFFF; border-top: 1px solid {BORDER}; color: {TEXT_MUTED}; font-family: {FONT}; font-size: 9pt; }}
QStatusBar::item {{ border: none; }}
QStatusBar QLabel {{ padding: 0 6px; }}
QLabel#Coords {{ color: #3A4B5A; font-family: {MONO}; }}
QLabel#Muted {{ color: {TEXT_MUTED}; }}
QLabel#Info {{ color: {TEXT_DARK}; font-weight: 600; }}
QLabel#Bad {{ color: {BAD}; font-weight: 600; }}
QLabel#Good {{ color: {GOOD}; }}
QProgressBar {{ border: none; background: #D6E5F2; border-radius: 3px; min-height: 6px; max-height: 6px; }}
QProgressBar::chunk {{ background: {ROLAVI_BLUE}; border-radius: 3px; }}
QPushButton {{ background: #FFFFFF; color: {TEXT_DARK}; border: 1px solid #C9D5E0; border-radius: 6px; padding: 2px 10px;
    font-weight: 600; }}
QPushButton:hover {{ border-color: {ROLAVI_BLUE}; }}
QPushButton:disabled {{ color: #A0AEBB; border-color: {BORDER}; }}
"""
