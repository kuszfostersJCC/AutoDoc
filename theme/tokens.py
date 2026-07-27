"""
theme/tokens.py — the single source of every color, radius, and border width.

Nothing else in AutoDoc may define one of these values. Python code that
paints manually (QStyledItemDelegate subclasses, notably
sessions_window/status_delegate.py) reads from TOKENS rather than hard-coding
a hex string — that is the ONLY sanctioned reason for Python to look at a
color at all.

Rules that are easy to break by accident:
  * One accent, one danger color. The accent appears only on the primary
    button, the selected table row, the active tab, and focus borders. If it
    shows up anywhere else, that's a defect.
  * Need a lighter grey? Use bg_subtle. If no token fits, the design is
    wrong — don't add a token to paper over it.
  * Disabled state is text_disabled on bg_subtle. Never Qt widget opacity;
    it causes repaint artifacts.
"""

TOKENS = {
    # Surfaces
    "bg_window":      "#F4F5F7",   # window / app chrome
    "bg_surface":     "#FFFFFF",   # content panels, table body, inputs
    "bg_subtle":      "#EDEFF2",   # table headers, inactive tabs, hover fills
    "bg_sunken":      "#E4E7EB",   # pressed fills, track grooves

    # Lines
    "border":         "#D3D8DE",   # every 1px border, default
    "border_strong":  "#B4BCC5",   # only where two bordered surfaces meet

    # Text
    "text":           "#1D2429",
    "text_muted":     "#5B6672",   # captions, help text, secondary labels
    "text_disabled":  "#9AA4AE",
    "text_on_accent": "#FFFFFF",

    # Accent (single)
    "accent":         "#2B5D8A",
    "accent_hover":   "#24507A",
    "accent_press":   "#1C4266",
    "accent_subtle":  "#DDE7F1",   # selected row background when unfocused
    "accent_focus":   "#4A85BE",   # focus border only

    # Danger (single)
    "danger":         "#A8322A",
    "danger_hover":   "#8E2822",
    "danger_subtle":  "#F6E3E1",

    # Geometry
    "radius_control": "4px",
    "radius_panel":   "6px",
    "border_width":   "1px",
}

# Type. Lives here rather than in main.py so that the §11 audit grep
# ("setFont|setPointSize|font-size — only theme/ allowed") stays clean:
# theme/loader.py::load_theme is the one place QApplication.setFont is called.
# 10pt ≈ 13px at 96 DPI, which is what the 28px row-height math in the style
# guide (§4, the descender rule) assumes.
FONT_FAMILY = "Segoe UI"
FONT_SIZE_PT = 10

