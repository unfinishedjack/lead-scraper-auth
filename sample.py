"""
Lead-gen style dashboard prototype, built with PySide6.
Sidebar + results table + business detail panel, dark theme with red accents.

NOTE: All contact info (phone numbers, websites, emails) below is placeholder
/ mock data made up for this prototype. The business names are real Baguio
landmarks, but the phone/site/email values are NOT verified real contact
details -- swap in real data once you wire up a backend.

Now with:
- Reviews column + review counts shown everywhere (table + detail panel)
- Real phone numbers / websites shown directly in the table (not just a checkmark)
- Sortable columns (click any header to sort, click again to reverse)
- Phone/website icons recolored (blue) so they don't clash with the
  green "Open" status color

Install with: pip install qtawesome PySide6
"""

import sys
import os
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QLabel,
    QLineEdit, QPushButton, QTableWidget, QTableWidgetItem, QHeaderView,
    QCheckBox, QProgressBar, QFrame, QAbstractItemView, QSizePolicy,
    QAbstractButton
)
from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QFont, QColor, QPainter, QPen, QBrush, QPainterPath, QPixmap

import qtawesome as qta

# --- Theme colors used for icon tinting (kept in sync with DARK_QSS below) ---
COLOR_MUTED = "#9ca3af"
COLOR_MUTED_DIM = "#6b7280"
COLOR_WHITE = "#ffffff"
COLOR_RED = "#f87171"
COLOR_RED_SOLID = "#ef4444"
COLOR_GREEN = "#4ade80"
COLOR_BLUE = "#60a5fa"   # used for phone / website now instead of green

# Path to your own logo file. Drop a PNG/SVG here (ideally square, transparent bg)
# and it will be used automatically. Falls back to the pin icon if not found.
LOGO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "logo.png")

SAMPLE_DATA = [
    {"name": "Burnham Park", "rating": 4.5, "reviews": 1280, "category": "Tourist Attraction",
     "status": "Open",
     "desc": "A famous urban park in the heart of Baguio City featuring a man-made lake, "
             "gardens, and various recreational activities.",
     "address": "Burnham Park, Baguio City, Benguet, Philippines",
     "hours": "5:00 AM - 10:00 PM", "phone_num": "(074) 442 3330",
     "site": "burnhampark.baguio.gov.ph", "email_addr": "info@burnhampark.baguio.gov.ph"},
    {"name": "Lion's Head", "rating": 4.5, "reviews": 940, "category": "Tourist Attraction",
     "status": "Open",
     "desc": "An iconic roadside stone carving of a lion's head along Kennon Road, "
             "a popular photo stop on the way into Baguio.",
     "address": "Kennon Road, Baguio City, Benguet, Philippines",
     "hours": "24 hours", "phone_num": "(074) 442 5108",
     "site": "visitbaguio.ph/lions-head", "email_addr": "info@visitbaguio.ph"},
    {"name": "Heritage Hill and Nature Park", "rating": 4.4, "reviews": 512, "category": "Tourist Attraction",
     "status": "Open",
     "desc": "A hillside nature park with cultural exhibits, gardens, and scenic viewpoints "
             "showcasing Cordilleran heritage.",
     "address": "Asin Road, Baguio City, Benguet, Philippines",
     "hours": "8:00 AM - 6:00 PM", "phone_num": "(074) 442 7761",
     "site": "heritagehillbaguio.ph", "email_addr": "hello@heritagehillbaguio.ph"},
    {"name": "Valley of Colors", "rating": 4.1, "reviews": 803, "category": "Tourist Attraction",
     "status": "Open",
     "desc": "A hillside community in Itogon known for its brightly painted houses, "
             "visible from a scenic viewing deck.",
     "address": "Itogon, Benguet, Philippines",
     "hours": "6:00 AM - 6:00 PM", "phone_num": "(074) 442 9013",
     "site": "valleyofcolors.ph", "email_addr": "contact@valleyofcolors.ph"},
    {"name": "Mines View Observation Deck", "rating": 4.3, "reviews": 655, "category": "Tourist Attraction",
     "status": "Closed",
     "desc": "A popular lookout point offering panoramic views of the old mining town "
             "of Itogon and the surrounding mountains.",
     "address": "Mines View Park Rd, Baguio City, Benguet, Philippines",
     "hours": "6:00 AM - 6:00 PM", "phone_num": "(074) 442 3781",
     "site": "minesviewbaguio.ph", "email_addr": "info@minesviewbaguio.ph"},
    {"name": "Tam-awan Village", "rating": 4.3, "reviews": 421, "category": "Tourist Attraction",
     "status": "Closed",
     "desc": "An artists' village built from reconstructed Ifugao and Kalinga huts, "
             "featuring art galleries and cultural performances.",
     "address": "Long-Long, Pinsao Proper, Baguio City, Benguet, Philippines",
     "hours": "8:00 AM - 6:00 PM", "phone_num": "(074) 446 2949",
     "site": "tam-awanvillage.ph", "email_addr": "visit@tam-awanvillage.ph"},
    {"name": "Our Lady of Lourdes Grotto", "rating": 4.6, "reviews": 1050, "category": "Tourist Attraction",
     "status": "Open",
     "desc": "A hilltop Catholic shrine reached by 252 steps, offering a peaceful "
             "pilgrimage site and city views.",
     "address": "Lourdes Grotto Rd, Baguio City, Benguet, Philippines",
     "hours": "6:00 AM - 6:00 PM", "phone_num": "(074) 442 4488",
     "site": "lourdesgrottobaguio.ph", "email_addr": "parish@lourdesgrottobaguio.ph"},
    {"name": "Rizal Park", "rating": 4.2, "reviews": 388, "category": "Tourist Attraction",
     "status": "Open",
     "desc": "A quiet public park near the city center featuring a monument to "
             "national hero Jose Rizal.",
     "address": "Rizal Park, Baguio City, Benguet, Philippines",
     "hours": "5:00 AM - 9:00 PM", "phone_num": "(074) 442 2156",
     "site": "baguio.gov.ph/rizal-park", "email_addr": "parks@baguio.gov.ph"},
    {"name": "Sunshine Park", "rating": 4.1, "reviews": 210, "category": "Tourist Attraction",
     "status": "Open",
     "desc": "A small family-friendly park with a giant swing and photo spots "
             "overlooking a mountain valley.",
     "address": "La Trinidad, Benguet, Philippines",
     "hours": "6:00 AM - 6:00 PM", "phone_num": "(074) 422 3390",
     "site": "sunshineparkbenguet.ph", "email_addr": "info@sunshineparkbenguet.ph"},
    {"name": "Wright Park", "rating": 4.4, "reviews": 567, "category": "Tourist Attraction",
     "status": "Closed",
     "desc": "A landscaped park known for its pine-lined pool and horseback riding "
             "along the Pool of Pines.",
     "address": "Wright Park, Baguio City, Benguet, Philippines",
     "hours": "6:00 AM - 6:00 PM", "phone_num": "(074) 442 3005",
     "site": "baguio.gov.ph/wright-park", "email_addr": "parks@baguio.gov.ph"},
]

DARK_QSS = """
QMainWindow, QWidget { background-color: #0d1117; color: #d1d5db; font-family: 'Segoe UI', sans-serif; font-size: 12px; }
#Sidebar { background-color: #111722; border-right: 1px solid #21262d; }
#SidebarTitle { color: white; font-size: 16px; font-weight: bold; padding: 4px 0 12px 0; }
QPushButton#NavActive { background-color: rgba(239,68,68,0.12); color: #f87171; border: none; border-left: 2px solid #ef4444; border-radius: 4px; text-align: left; padding: 8px 10px 8px 8px; font-weight: 500; }
QPushButton#NavItem { background-color: transparent; color: #9ca3af; border: none; border-left: 2px solid transparent; border-radius: 6px; text-align: left; padding: 8px 10px 8px 8px; }
QPushButton#NavItem:hover { background-color: #1c2128; }
#NavDivider { background-color: #21262d; max-height: 1px; min-height: 1px; margin: 8px 4px; }
#CreditsBox { background-color: #0d1117; border: 1px solid #21262d; border-radius: 8px; padding: 10px; }
#HeaderTitle { color: white; font-size: 18px; font-weight: bold; }
#HeaderSub { color: #6b7280; font-size: 11px; }
QLineEdit { background-color: #111722; border: 1px solid #21262d; border-radius: 5px; padding: 6px 10px; color: #d1d5db; }
QLineEdit:focus { border: 1px solid #4b5563; }
QPushButton#OutlineBtn { background-color: transparent; border: 1px solid #30363d; border-radius: 5px; padding: 6px 12px; color: #d1d5db; }
QPushButton#OutlineBtn:hover { background-color: #1c2128; }
QPushButton#RedBtn { background-color: #ef4444; border: none; border-radius: 5px; padding: 6px 14px; color: white; font-weight: 500; }
QPushButton#RedBtn:hover { background-color: #dc2626; }
QTableWidget { background-color: #0d1117; border: none; gridline-color: #161b22; selection-background-color: #1c2128; }
QHeaderView::section { background-color: #0d1117; color: #6b7280; border: none; border-bottom: 1px solid #21262d; padding: 6px; font-weight: 500; }
QHeaderView::section:hover { color: #d1d5db; background-color: #10151d; }
QTableWidget::item { border-bottom: 1px solid #161b22; padding: 4px; }
#DetailPanel { background-color: #0d1117; border-left: 1px solid #21262d; }
#DetailHeader { color: white; font-size: 13px; font-weight: 600; }
QPushButton#DetailCloseBtn { background-color: transparent; border: none; border-radius: 4px; padding: 2px; }
QPushButton#DetailCloseBtn:hover { background-color: #1c2128; }
#BizName { color: white; font-size: 15px; font-weight: bold; }
#BizMeta { color: #9ca3af; font-size: 11px; }
#SectionLabel { color: white; font-size: 11px; font-weight: 600; }
#AboutText { color: #9ca3af; font-size: 11px; }
QProgressBar { background-color: #21262d; border-radius: 2px; min-height: 4px; max-height: 4px; text-align: center; color: transparent; }
QProgressBar::chunk { background-color: #ef4444; border-radius: 2px; }
"""

# Column layout for the results table.
# key is None for columns that aren't sortable (row #, checkbox).
COLUMNS = [
    {"label": "#", "key": None},
    {"label": "", "key": None},
    {"label": "Business Name", "key": "name"},
    {"label": "Rating", "key": "rating"},
    {"label": "Reviews", "key": "reviews"},
    {"label": "Category", "key": "category"},
    {"label": "Phone", "key": "phone_num"},
    {"label": "Website", "key": "site"},
    {"label": "Status", "key": "status"},
]
COL_ROWNUM, COL_CHECK, COL_NAME, COL_RATING, COL_REVIEWS, COL_CATEGORY, COL_PHONE, COL_WEBSITE, COL_STATUS = range(9)


def status_pill(status: str) -> QWidget:
    """
    Small status badge (colored background only around the text/icon,
    not the whole cell) centered inside a transparent outer wrapper.
    """
    is_open = status == "Open"
    accent = COLOR_GREEN if is_open else COLOR_RED
    bg = "rgba(34,197,94,0.12)" if is_open else "rgba(239,68,68,0.12)"

    dot_lbl = QLabel()
    dot_lbl.setStyleSheet("background: transparent;")
    dot_lbl.setPixmap(qta.icon('fa5s.circle', color=accent).pixmap(8, 8))

    text_lbl = QLabel(status)
    text_lbl.setStyleSheet(f"color: {accent}; font-size: 11px; background: transparent;")

    # Inner pill: sized to fit its content only, colored background + rounded corners
    pill = QFrame()
    pill.setStyleSheet(f"background-color: {bg}; border-radius: 4px;")
    pill.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
    pill_layout = QHBoxLayout(pill)
    pill_layout.setContentsMargins(8, 3, 8, 3)
    pill_layout.setSpacing(5)
    pill_layout.addWidget(dot_lbl)
    pill_layout.addWidget(text_lbl)

    # Outer wrapper: fully transparent, just centers the pill in the cell
    outer = QWidget()
    outer.setStyleSheet("background: transparent;")
    outer_layout = QHBoxLayout(outer)
    outer_layout.setContentsMargins(0, 0, 0, 0)
    outer_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
    outer_layout.addWidget(pill)

    return outer


class LeadCheckBox(QAbstractButton):
    """
    Fully hand-painted checkbox. QPushButton + QSS was unreliable across
    platforms/native styles (some styles keep painting their own button
    background/frame underneath the stylesheet, leaving only the border
    color visibly overridden). Painting it ourselves with QPainter removes
    that ambiguity entirely -- this will look identical everywhere.
    """
    def __init__(self, checked=True, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(checked)
        self.setFixedSize(16, 16)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = QRectF(1, 1, self.width() - 2, self.height() - 2)

        if self.isChecked():
            painter.setBrush(QBrush(QColor(COLOR_RED_SOLID)))
            painter.setPen(QPen(QColor(COLOR_RED_SOLID), 1))
        else:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            border_color = "#6b7280" if self.underMouse() else "#4b5563"
            painter.setPen(QPen(QColor(border_color), 1.2))

        painter.drawRoundedRect(rect, 4, 4)

        if self.isChecked():
            pen = QPen(QColor("white"), 1.6)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            path = QPainterPath()
            path.moveTo(4.2, 8.4)
            path.lineTo(6.8, 11.2)
            path.lineTo(11.8, 4.8)
            painter.drawPath(path)

        painter.end()

    def enterEvent(self, event):
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.update()
        super().leaveEvent(event)


def checkbox_cell(checked=True) -> QWidget:
    """Centered wrapper so the checkbox sits nicely inside a table cell."""
    wrap = QWidget()
    wrap.setStyleSheet("background: transparent;")
    row = QHBoxLayout(wrap)
    row.setContentsMargins(8, 0, 0, 0)
    row.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
    box = LeadCheckBox(checked)
    box.toggled.connect(lambda _checked, b=box: b.update())
    row.addWidget(box)
    return wrap


def rating_cell(rating: float, star_color="#facc15") -> QWidget:
    """Rating number followed by a star icon to its right (used in the table)."""
    wrap = QWidget()
    wrap.setStyleSheet("background: transparent;")
    row = QHBoxLayout(wrap)
    row.setContentsMargins(6, 0, 6, 0)
    row.setSpacing(5)
    num_lbl = QLabel(str(rating))
    num_lbl.setStyleSheet("color: #d1d5db; font-size: 12px; background: transparent;")
    row.addWidget(num_lbl)
    star_lbl = QLabel()
    star_lbl.setPixmap(qta.icon('fa5s.star', color=star_color).pixmap(11, 11))
    row.addWidget(star_lbl)
    row.addStretch()
    return wrap


def website_cell(text: str, color=COLOR_BLUE, size=12) -> QWidget:
    """Website shown as blue text with a small icon (the only blue contact field)."""
    wrap = QWidget()
    wrap.setStyleSheet("background: transparent;")
    row = QHBoxLayout(wrap)
    row.setContentsMargins(6, 0, 6, 0)
    row.setSpacing(6)
    icon_lbl = QLabel()
    icon_lbl.setStyleSheet("background: transparent;")
    icon_lbl.setPixmap(qta.icon('fa5s.globe', color=color).pixmap(size, size))
    row.addWidget(icon_lbl)
    text_lbl = QLabel(text)
    text_lbl.setStyleSheet(f"color: {color}; font-size: 11px; background: transparent;")
    text_lbl.setToolTip(text)
    row.addWidget(text_lbl, stretch=1)
    return wrap


def icon_label(icon_name: str, text: str, color=COLOR_MUTED, size=13) -> QWidget:
    """Small helper: icon + text row, used in the detail panel contact rows."""
    wrap = QWidget()
    row = QHBoxLayout(wrap)
    row.setContentsMargins(0, 2, 0, 2)
    row.setSpacing(8)

    icon_lbl = QLabel()
    icon_lbl.setPixmap(qta.icon(icon_name, color=color).pixmap(size, size))
    icon_lbl.setFixedWidth(size)
    row.addWidget(icon_lbl)

    text_lbl = QLabel(text)
    text_lbl.setWordWrap(True)
    text_lbl.setStyleSheet(f"color: {color}; font-size: 11px;")
    row.addWidget(text_lbl, stretch=1)

    return wrap


class DetailPanel(QFrame):
    """
    Shows details for the currently-selected row.

    IMPORTANT: every widget here is built exactly once in __init__.
    show_business() only updates text/icons/styles on those existing
    widgets -- it never deletes or recreates them. Rebuilding the whole
    widget tree from scratch on every single row click (the previous
    approach) is what caused the 'free(): invalid pointer' crash: rapid
    clicks queued up deleteLater() calls faster than Qt/shiboken could
    safely process them, leading to use-after-free. Updating in place
    avoids that class of bug entirely and is also much cheaper.
    """
    def __init__(self):
        super().__init__()
        self.setObjectName("DetailPanel")
        self.setFixedWidth(280)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 12, 16, 12)
        outer.setSpacing(6)

        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        header = QLabel("Business Details")
        header.setObjectName("DetailHeader")
        header_row.addWidget(header)
        header_row.addStretch()
        self.close_btn = QPushButton()
        self.close_btn.setObjectName("DetailCloseBtn")
        self.close_btn.setIcon(qta.icon('fa5s.times', color=COLOR_MUTED))
        self.close_btn.setFixedSize(20, 20)
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_btn.setToolTip("Close")
        self.close_btn.clicked.connect(self.close_panel)
        header_row.addWidget(self.close_btn)
        outer.addLayout(header_row)

        # --- empty-state placeholder (shown before any row is selected) ---
        self.placeholder = QLabel("Select a row to see details")
        self.placeholder.setStyleSheet("color: #6b7280; font-size: 11px;")
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(self.placeholder)

        # --- content widgets (built once, hidden until a row is picked) ---
        self.content = QWidget()
        content_layout = QVBoxLayout(self.content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(6)

        photo = QFrame()
        photo.setFixedHeight(110)
        photo.setStyleSheet(
            "background: qlineargradient(x1:0, y1:0, x2:1, y2:1, "
            "stop:0 #14532d, stop:1 #1e3a8a); border-radius: 8px;"
        )
        content_layout.addWidget(photo)

        self.name_lbl = QLabel()
        self.name_lbl.setObjectName("BizName")
        content_layout.addWidget(self.name_lbl)

        # Rating number first, then star icon to its right, then review count
        rating_row = QHBoxLayout()
        rating_row.setContentsMargins(0, 0, 0, 0)
        rating_row.setSpacing(5)
        self.rating_num_lbl = QLabel()
        self.rating_num_lbl.setObjectName("BizMeta")
        rating_row.addWidget(self.rating_num_lbl)
        star_lbl = QLabel()
        star_lbl.setPixmap(qta.icon('fa5s.star', color="#facc15").pixmap(12, 12))
        rating_row.addWidget(star_lbl)
        self.reviews_lbl = QLabel()
        self.reviews_lbl.setObjectName("BizMeta")
        rating_row.addWidget(self.reviews_lbl)
        rating_row.addStretch()
        content_layout.addLayout(rating_row)

        self.category_lbl = QLabel()
        self.category_lbl.setObjectName("BizMeta")
        content_layout.addWidget(self.category_lbl)

        # Status pill: dot + text live inside a QFrame whose background
        # style gets updated in place (no widget recreation needed).
        self.status_frame = QFrame()
        self.status_frame.setFixedWidth(90)
        status_layout = QHBoxLayout(self.status_frame)
        status_layout.setContentsMargins(8, 3, 8, 3)
        status_layout.setSpacing(5)
        self.status_dot_lbl = QLabel()
        self.status_dot_lbl.setStyleSheet("background: transparent;")
        status_layout.addWidget(self.status_dot_lbl)
        self.status_text_lbl = QLabel()
        status_layout.addWidget(self.status_text_lbl)
        content_layout.addWidget(self.status_frame)

        content_layout.addSpacing(8)

        # Contact rows: icon per row is fixed (phone/globe/envelope/pin),
        # only the text next to it changes per business.
        self.phone_lbl = self._add_contact_row(content_layout, 'fa5s.phone-alt', COLOR_BLUE)
        self.site_lbl = self._add_contact_row(content_layout, 'fa5s.globe', COLOR_BLUE)
        self.email_lbl = self._add_contact_row(content_layout, 'fa5s.envelope', COLOR_MUTED)
        self.address_lbl = self._add_contact_row(content_layout, 'fa5s.map-marker-alt', COLOR_MUTED)

        content_layout.addSpacing(10)
        about_label = QLabel("About")
        about_label.setObjectName("SectionLabel")
        content_layout.addWidget(about_label)

        self.about_text_lbl = QLabel()
        self.about_text_lbl.setObjectName("AboutText")
        self.about_text_lbl.setWordWrap(True)
        content_layout.addWidget(self.about_text_lbl)

        content_layout.addStretch()

        btn_row = QHBoxLayout()
        maps_btn = QPushButton(qta.icon('fa5s.map-marked-alt', color=COLOR_MUTED), " Open in Maps")
        maps_btn.setObjectName("OutlineBtn")
        copy_btn = QPushButton(qta.icon('fa5s.copy', color=COLOR_MUTED), " Copy Details")
        copy_btn.setObjectName("OutlineBtn")
        btn_row.addWidget(maps_btn)
        btn_row.addWidget(copy_btn)
        content_layout.addLayout(btn_row)

        outer.addWidget(self.content)

        self.build_empty()

    def _add_contact_row(self, layout, icon_name, color, size=13) -> QLabel:
        """Adds a fixed icon + an updateable text QLabel; returns the label."""
        row = QHBoxLayout()
        row.setContentsMargins(0, 2, 0, 2)
        row.setSpacing(8)

        icon_lbl = QLabel()
        icon_lbl.setPixmap(qta.icon(icon_name, color=color).pixmap(size, size))
        icon_lbl.setFixedWidth(size)
        row.addWidget(icon_lbl)

        text_lbl = QLabel()
        text_lbl.setWordWrap(True)
        text_lbl.setStyleSheet(f"color: {color}; font-size: 11px;")
        row.addWidget(text_lbl, stretch=1)

        layout.addLayout(row)
        return text_lbl

    def build_empty(self):
        self.placeholder.setVisible(True)
        self.content.setVisible(False)

    def close_panel(self):
        """Hides the whole panel (table area reclaims the space). It
        reappears automatically the next time a row is clicked."""
        self.setVisible(False)

    def show_business(self, biz: dict):
        # Clicking a row always brings the panel back if it was closed.
        self.setVisible(True)
        self.placeholder.setVisible(False)
        self.content.setVisible(True)

        self.name_lbl.setText(biz["name"])
        self.rating_num_lbl.setText(str(biz["rating"]))
        self.reviews_lbl.setText(f"({biz.get('reviews', 0):,} reviews)")
        self.category_lbl.setText(biz["category"])

        is_open = biz["status"] == "Open"
        accent = COLOR_GREEN if is_open else COLOR_RED
        bg = "rgba(34,197,94,0.12)" if is_open else "rgba(239,68,68,0.12)"
        self.status_frame.setStyleSheet(f"background-color: {bg}; border-radius: 4px;")
        self.status_dot_lbl.setPixmap(qta.icon('fa5s.circle', color=accent).pixmap(8, 8))
        self.status_text_lbl.setText("Open now" if is_open else "Closed")
        self.status_text_lbl.setStyleSheet(f"color: {accent}; font-size: 11px; background: transparent;")

        self.phone_lbl.setText(biz.get("phone_num", "(0) 000 0000"))
        self.site_lbl.setText(biz.get("site", "example-site.com"))
        self.email_lbl.setText(biz.get("email_addr", "contact@example.com"))
        self.address_lbl.setText(biz.get("address", "Baguio City, Benguet, Philippines"))

        self.about_text_lbl.setText(biz.get("desc", "No description available yet."))


class Dashboard(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("LeadScout - Restaurants in Baguio")
        self.setWindowIcon(qta.icon('fa5s.map-marker-alt', color=COLOR_RED_SOLID))
        self.resize(1440, 760)

        # Working copy of the data so sorting doesn't mutate SAMPLE_DATA,
        # and so on_row_clicked always matches what's currently on screen.
        self.current_data = list(SAMPLE_DATA)
        self.sort_column = None
        self.sort_ascending = True

        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.detail_panel = DetailPanel()

        root.addWidget(self.build_sidebar())
        root.addWidget(self.build_main(), stretch=1)
        root.addWidget(self.detail_panel)

    def build_sidebar(self) -> QWidget:
        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(200)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(14, 14, 14, 14)

        title_icon = QLabel()
        title_icon.setStyleSheet("background: transparent;")
        title_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo_pixmap = QPixmap(LOGO_PATH)
        if not logo_pixmap.isNull():
            # Your logo file, scaled to fit the sidebar header
            title_icon.setPixmap(
                logo_pixmap.scaledToHeight(98, Qt.TransformationMode.SmoothTransformation)
            )
        else:
            # Fallback placeholder icon if assets/logo.png isn't found
            title_icon.setPixmap(qta.icon('fa5s.map-marker-alt', color=COLOR_RED_SOLID).pixmap(48, 48))
        layout.addWidget(title_icon)
        layout.addSpacing(24)

        nav_items_top = [
            ("fa5s.th-large", "Dashboard", False),
            ("fa5s.search", "Search Leads", True),
            ("fa5s.bookmark", "Saved Searches", False),
            ("fa5s.file-export", "Exports", False),
            ("fa5s.credit-card", "Billing", False),
        ]
        for icon_name, text, active in nav_items_top:
            color = COLOR_RED if active else COLOR_MUTED
            btn = QPushButton(qta.icon(icon_name, color=color), " " + text)
            btn.setObjectName("NavActive" if active else "NavItem")
            layout.addWidget(btn)

        # Divider between the search-related nav items and the settings/support group
        divider = QFrame()
        divider.setObjectName("NavDivider")
        divider.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(divider)

        nav_items_bottom = [
            ("fa5s.cog", "Settings", False),
            ("fa5s.life-ring", "Help & Support", False),
        ]
        for icon_name, text, active in nav_items_bottom:
            color = COLOR_RED if active else COLOR_MUTED
            btn = QPushButton(qta.icon(icon_name, color=color), " " + text)
            btn.setObjectName("NavActive" if active else "NavItem")
            layout.addWidget(btn)

        layout.addStretch()

        credits_box = QFrame()
        credits_box.setObjectName("CreditsBox")
        cb_layout = QVBoxLayout(credits_box)
        cb_label_row = QHBoxLayout()
        cb_icon = QLabel()
        cb_icon.setPixmap(qta.icon('fa5s.coins', color="#facc15").pixmap(11, 11))
        cb_label_row.addWidget(cb_icon)
        cb_label = QLabel("Credits Remaining")
        cb_label.setStyleSheet("color: #6b7280; font-size: 10px;")
        cb_label_row.addWidget(cb_label)
        cb_label_row.addStretch()
        cb_value = QLabel("100,002")
        cb_value.setStyleSheet("color: white; font-size: 16px; font-weight: bold;")
        bar = QProgressBar()
        bar.setValue(70)
        bar.setTextVisible(False)
        cb_layout.addLayout(cb_label_row)
        cb_layout.addWidget(cb_value)
        cb_layout.addWidget(bar)
        layout.addWidget(credits_box)

        return sidebar

    def build_main(self) -> QWidget:
        main = QWidget()
        layout = QVBoxLayout(main)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)

        header_row = QHBoxLayout()
        title_col = QVBoxLayout()
        title = QLabel("Restaurants in Baguio")
        title.setObjectName("HeaderTitle")
        sub = QLabel(f"{len(SAMPLE_DATA)} businesses found")
        sub.setObjectName("HeaderSub")
        title_col.addWidget(title)
        title_col.addWidget(sub)
        header_row.addLayout(title_col)
        header_row.addStretch()

        header_buttons = [
            ("fa5s.filter", "Filters", "OutlineBtn", COLOR_MUTED),
            ("fa5s.columns", "Columns", "OutlineBtn", COLOR_MUTED),
            ("fa5s.download", "Export", "RedBtn", COLOR_WHITE),
        ]
        for icon_name, label, obj_name, icon_color in header_buttons:
            btn = QPushButton(qta.icon(icon_name, color=icon_color), " " + label)
            btn.setObjectName(obj_name)
            header_row.addWidget(btn)
        layout.addLayout(header_row)

        search = QLineEdit()
        search.setPlaceholderText("Search in results...")
        search.addAction(qta.icon('fa5s.search', color=COLOR_MUTED_DIM), QLineEdit.ActionPosition.LeadingPosition)
        layout.addWidget(search)

        self.table = QTableWidget()
        self.table.setColumnCount(len(COLUMNS))
        self.table.setHorizontalHeaderLabels([c["label"] for c in COLUMNS])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setRowCount(len(self.current_data))

        header = self.table.horizontalHeader()
        # Row-number/checkbox columns stay a fixed narrow width; every other
        # column is user-resizable (drag the column border to resize) with
        # sensible starting widths. setStretchLastSection makes the very
        # last column (Status) automatically absorb any leftover width --
        # e.g. on a wide window, or once the detail panel gets closed and
        # frees up space -- while still letting the user drag it narrower/
        # wider like any other column.
        header.setSectionResizeMode(COL_ROWNUM, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(COL_ROWNUM, 30)
        header.setSectionResizeMode(COL_CHECK, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(COL_CHECK, 30)
        for col, width in [
            (COL_NAME, 220),
            (COL_RATING, 70),
            (COL_REVIEWS, 90),
            (COL_CATEGORY, 140),
            (COL_PHONE, 150),
            (COL_WEBSITE, 190),
            (COL_STATUS, 90),
        ]:
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
            self.table.setColumnWidth(col, width)
        header.setStretchLastSection(True)

        # We handle sorting ourselves (see handle_sort) so that the custom
        # cell widgets (checkboxes, pills, icons) always follow their data
        # correctly -- QTableWidget's built-in sortItems() does not reliably
        # move cell widgets along with their rows.
        header.setSectionsClickable(True)
        header.setSortIndicatorShown(True)
        header.sectionClicked.connect(self.handle_sort)

        self.populate_table(self.current_data)

        self.table.cellClicked.connect(self.on_row_clicked)
        layout.addWidget(self.table)

        footer = QHBoxLayout()
        self.footer_label = QLabel(f"Showing 1 to {len(SAMPLE_DATA)} of 161 results")
        self.footer_label.setStyleSheet("color: #6b7280; font-size: 11px;")
        footer.addWidget(self.footer_label)
        footer.addStretch()
        layout.addLayout(footer)

        return main

    def populate_table(self, data):
        """(Re)draws all rows from `data` -- used both for the initial load
        and every time the user re-sorts a column."""
        self.table.setRowCount(len(data))

        for row, biz in enumerate(data):
            row_num_item = QTableWidgetItem(str(row + 1))
            row_num_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row, COL_ROWNUM, row_num_item)

            self.table.setCellWidget(row, COL_CHECK, checkbox_cell(checked=True))

            self.table.setItem(row, COL_NAME, QTableWidgetItem(biz["name"]))

            # Rating: number first, star icon to its right
            self.table.setCellWidget(row, COL_RATING, rating_cell(biz["rating"]))

            # Reviews: plain number, no icon, no thousands separator
            self.table.setItem(row, COL_REVIEWS, QTableWidgetItem(str(biz.get("reviews", 0))))

            self.table.setItem(row, COL_CATEGORY, QTableWidgetItem(biz["category"]))

            # Phone: plain text, no icon, normal (default) text color
            self.table.setItem(row, COL_PHONE, QTableWidgetItem(biz.get("phone_num", "--")))

            # Website: the only contact field that keeps the icon + blue color
            self.table.setCellWidget(
                row, COL_WEBSITE,
                website_cell(biz.get("site", "--"), color=COLOR_BLUE)
            )

            self.table.setCellWidget(row, COL_STATUS, status_pill(biz["status"]))

        # Pre-select first row and show it in the detail panel
        if data:
            self.table.selectRow(0)
            self.detail_panel.show_business(data[0])
        else:
            self.detail_panel.build_empty()

    def handle_sort(self, column: int):
        key = COLUMNS[column]["key"]
        if key is None:
            return  # row-number / checkbox columns aren't sortable

        if self.sort_column == column:
            self.sort_ascending = not self.sort_ascending
        else:
            self.sort_column = column
            self.sort_ascending = True

        self.current_data = sorted(
            self.current_data,
            key=lambda biz: biz.get(key, ""),
            reverse=not self.sort_ascending,
        )

        order = Qt.SortOrder.AscendingOrder if self.sort_ascending else Qt.SortOrder.DescendingOrder
        self.table.horizontalHeader().setSortIndicator(column, order)

        self.populate_table(self.current_data)

    def on_row_clicked(self, row, _col):
        self.detail_panel.show_business(self.current_data[row])


def main():
    app = QApplication(sys.argv)
    # Fusion style respects QSS backgrounds fully; native styles (esp. Windows
    # Vista style) can ignore background-color on QPushButton and only apply
    # the border, which is why filled buttons/checkboxes can look hollow.
    app.setStyle("Fusion")
    app.setStyleSheet(DARK_QSS)
    window = Dashboard()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()