"""Liquid-glass theming for the overlay.

The panel reads as glass without any native compositing - see the note in
:mod:`typing_simulator.ui.macos_overlay` for why an ``NSVisualEffectView`` is
not used.  Three details do the work:

* a **vertical gradient** on the panel, lighter at the top, which reads as
  light falling across a curved glass surface.  It is kept close to opaque:
  the panel floats over arbitrary documents, and text bleeding through from
  behind makes the controls unreadable;
* a **bright hairline border**, brightest at the top edge, standing in for the
  specular rim Apple's material has;
* **frosted inner surfaces** - fields and buttons are white at low alpha rather
  than solid fills, so the blurred backdrop tints them.

Everything still looks right if the blur cannot be installed; it degrades to a
flat translucent panel.

Semantic colours are applied through a ``tone`` dynamic property rather than
inline stylesheets, so a light/dark switch restyles every label automatically:

    label.setProperty("tone", "ok")
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from string import Template

#: Corner radius of the glass panel; the native blur layer uses the same value.
PANEL_RADIUS = 22

#: Qt layout margins reserved around the panel for the drop shadow.
PANEL_MARGINS = (20, 16, 20, 24)


@dataclass(frozen=True, slots=True)
class Palette:
    panel_top: str
    panel_mid: str
    panel_bottom: str
    rim: str
    rim_soft: str
    text: str
    muted: str
    field: str
    field_border: str
    field_focus: str
    control: str
    control_hover: str
    accent: str
    accent_hover: str
    on_accent: str
    ok: str
    warn: str
    bad: str
    danger_fill: str
    danger_border: str
    track: str
    banner: str
    banner_border: str
    shadow_alpha: int


#: Dark glass.  Alphas are low on purpose - the blur behind supplies the body.
DARK = Palette(
    panel_top="rgba(74, 74, 82, 232)",
    panel_mid="rgba(44, 44, 50, 240)",
    panel_bottom="rgba(28, 28, 32, 246)",
    rim="rgba(255, 255, 255, 56)",
    rim_soft="rgba(255, 255, 255, 20)",
    text="#F5F5F7",
    muted="rgba(235, 235, 245, 150)",
    field="rgba(255, 255, 255, 20)",
    field_border="rgba(255, 255, 255, 30)",
    field_focus="rgba(10, 132, 255, 200)",
    control="rgba(255, 255, 255, 30)",
    control_hover="rgba(255, 255, 255, 52)",
    accent="rgba(10, 132, 255, 235)",
    accent_hover="rgba(64, 156, 255, 245)",
    on_accent="#FFFFFF",
    ok="#3DDC69",
    warn="#FFB340",
    bad="#FF6961",
    danger_fill="rgba(255, 69, 58, 42)",
    danger_border="rgba(255, 105, 97, 120)",
    track="rgba(255, 255, 255, 28)",
    banner="rgba(255, 159, 10, 38)",
    banner_border="rgba(255, 179, 64, 130)",
    shadow_alpha=170,
)

#: Light glass.
LIGHT = Palette(
    panel_top="rgba(255, 255, 255, 240)",
    panel_mid="rgba(249, 249, 252, 244)",
    panel_bottom="rgba(236, 236, 242, 248)",
    rim="rgba(255, 255, 255, 220)",
    rim_soft="rgba(0, 0, 0, 26)",
    text="#1D1D1F",
    muted="rgba(60, 60, 67, 160)",
    field="rgba(255, 255, 255, 190)",
    field_border="rgba(0, 0, 0, 32)",
    field_focus="rgba(0, 122, 255, 200)",
    control="rgba(0, 0, 0, 16)",
    control_hover="rgba(0, 0, 0, 28)",
    accent="rgba(0, 122, 255, 240)",
    accent_hover="rgba(20, 138, 255, 250)",
    on_accent="#FFFFFF",
    ok="#1D8A34",
    warn="#A85800",
    bad="#C9110F",
    danger_fill="rgba(215, 0, 21, 26)",
    danger_border="rgba(201, 17, 15, 90)",
    track="rgba(0, 0, 0, 22)",
    banner="rgba(255, 179, 64, 60)",
    banner_border="rgba(168, 88, 0, 90)",
    shadow_alpha=90,
)


_SHEET = Template(
    """
#panel {
    background: qlineargradient(x1:0, y1:0, x2:0.4, y2:1,
        stop:0 $panel_top, stop:0.45 $panel_mid, stop:1 $panel_bottom);
    border: 1px solid $rim_soft;
    border-top: 1px solid $rim;
    border-radius: ${radius}px;
}

QWidget { color: $text; font-size: 13px; background: transparent; }
QLabel { color: $text; font-size: 12px; background: transparent; }
QLabel#title { font-size: 13px; font-weight: 600; letter-spacing: 0.2px; }
QLabel[tone="muted"], QLabel[tone="caption"] { color: $muted; font-size: 11px; }
QLabel[tone="ok"] { color: $ok; font-size: 11px; }
QLabel[tone="warn"] { color: $warn; font-size: 11px; }
QLabel[tone="bad"] { color: $bad; font-size: 11px; }

#banner {
    background-color: $banner;
    border: 1px solid $banner_border;
    border-radius: 12px;
}

#divider { background-color: $rim_soft; border: none; }

QPlainTextEdit {
    background-color: $field;
    border: 1px solid $field_border;
    border-radius: 12px;
    color: $text;
    padding: 9px 10px;
    font-size: 12px;
    selection-background-color: $accent;
    selection-color: $on_accent;
}
QSpinBox, QDoubleSpinBox, QComboBox, QLineEdit {
    background-color: $field;
    border: 1px solid $field_border;
    border-radius: 9px;
    color: $text;
    padding: 4px 8px;
    min-height: 20px;
    font-size: 12px;
}
QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus, QLineEdit:focus,
QPlainTextEdit:focus { border: 1px solid $field_focus; }
QComboBox::drop-down { border: none; width: 18px; }
QComboBox QAbstractItemView {
    background-color: $panel_bottom;
    border: 1px solid $rim_soft;
    border-radius: 10px;
    selection-background-color: $accent;
    selection-color: $on_accent;
    color: $text;
    padding: 4px;
}

/* Steppers and the checkbox indicator stay unstyled so Qt draws the real
   macOS controls, which already track the system appearance and accent. */
QCheckBox { color: $text; font-size: 12px; spacing: 7px; background: transparent; }

QPushButton {
    background-color: $control;
    border: 1px solid $rim_soft;
    border-radius: 15px;          /* pill: half of the 30px min-height */
    color: $text;
    padding: 7px 16px;
    font-size: 12px;
    font-weight: 500;
}
QPushButton:hover:enabled { background-color: $control_hover; }
QPushButton:disabled { color: $muted; background-color: $control; border-color: transparent; }
QPushButton#primary {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 $accent_hover, stop:1 $accent);
    border: 1px solid $rim_soft;
    color: $on_accent;
    font-weight: 600;
}
QPushButton#primary:hover:enabled { background: $accent_hover; }
QPushButton#primary:disabled {
    background: $control;
    color: $muted;
    border-color: transparent;
}
QPushButton#danger {
    background-color: $danger_fill;
    border: 1px solid $danger_border;
    color: $bad;
}
QPushButton#danger:disabled {
    background-color: $control;
    color: $muted;
    border-color: transparent;
}
QPushButton#chip {
    background-color: $control;
    border: 1px solid $banner_border;
    border-radius: 11px;
    padding: 4px 11px;
    font-size: 11px;
}
QPushButton#chip:hover:enabled { background-color: $control_hover; }
QPushButton#glyph {
    background: transparent;
    color: $muted;
    border: none;
    border-radius: 10px;
    padding: 0px;
    font-size: 13px;
}
QPushButton#glyph:hover:enabled { background-color: $control_hover; color: $text; }

QProgressBar {
    background-color: $track;
    border: none;
    border-radius: 3px;
    height: 6px;
    text-align: center;
    color: transparent;
}
QProgressBar::chunk {
    border-radius: 3px;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 $accent, stop:1 $accent_hover);
}

QToolTip {
    background-color: $panel_bottom;
    color: $text;
    border: 1px solid $rim_soft;
    border-radius: 8px;
    padding: 5px 8px;
}
"""
)


def stylesheet(palette: Palette) -> str:
    values = asdict(palette)
    values["radius"] = PANEL_RADIUS
    return _SHEET.substitute(values)
