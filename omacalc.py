#!/usr/bin/env python3
"""OmaCalc — Tray icon calculator for Omarchy."""

import math
import os
import subprocess
import ast
import re
import signal
import sys

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Gtk, Gdk, GLib, Gio, Gtk4LayerShell

import dbus
import dbus.service
import dbus.mainloop.glib

# ---------------------------------------------------------------------------
# Theme
# ---------------------------------------------------------------------------

DEFAULT_COLORS = {
    "background": "#1a1b26",
    "foreground": "#a9b1d6",
    "accent": "#7aa2f7",
    "color0": "#32344a",
    "color1": "#f7768e",
    "color2": "#9ece6a",
    "color3": "#e0af68",
    "color5": "#ad8ee6",
}


def load_theme_colors():
    path = os.path.expanduser("~/.config/omarchy/current/theme/colors.toml")
    colors = dict(DEFAULT_COLORS)
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    key, _, val = line.partition("=")
                    key = key.strip()
                    val = val.strip().strip('"').strip("'")
                    if val.startswith("#"):
                        colors[key] = val
    except FileNotFoundError:
        pass
    return colors


def get_hyprland_rounding():
    try:
        out = subprocess.check_output(
            ["hyprctl", "getoption", "decoration:rounding"],
            text=True, timeout=2,
        )
        for line in out.splitlines():
            if line.strip().startswith("int:"):
                return int(line.split(":")[1].strip())
    except Exception:
        pass
    return 0


def hex_to_rgba(h):
    """Convert #RRGGBB to (r,g,b,a) bytes."""
    h = h.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 255)


def get_hyprland_border_colors():
    """Get active/inactive border colors from Hyprland."""
    active = inactive = None
    for name, target in [("general:col.active_border", "active"),
                         ("general:col.inactive_border", "inactive")]:
        try:
            out = subprocess.check_output(
                ["hyprctl", "getoption", name], text=True, timeout=2,
            )
            for line in out.splitlines():
                line = line.strip()
                if line.startswith("custom type:"):
                    # format: "custom type: ffRRGGBB 0deg" (ARGB)
                    argb = line.split(":")[1].strip().split()[0]
                    if len(argb) == 8:
                        r, g, b = argb[2:4], argb[4:6], argb[6:8]
                        a = int(argb[0:2], 16) / 255.0
                        hexcol = f"#{r}{g}{b}"
                        if target == "active":
                            active = (hexcol, a)
                        else:
                            inactive = (hexcol, a)
        except Exception:
            pass
    return active, inactive


def hex_to_css_rgba(hexcol, alpha):
    """Convert #RRGGBB + alpha float to rgba() string."""
    h = hexcol.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r}, {g}, {b}, {alpha:.2f})"


def build_css(colors, rounding):
    bg = colors.get("background", "#1a1b26")
    fg = colors.get("foreground", "#a9b1d6")
    accent = colors.get("accent", "#7aa2f7")
    green = colors.get("color2", "#9ece6a")

    active, inactive = get_hyprland_border_colors()
    if active:
        active_border = hex_to_css_rgba(*active)
    else:
        active_border = accent
    if inactive:
        inactive_border = hex_to_css_rgba(*inactive)
    else:
        inactive_border = f"rgba(89, 89, 89, 0.67)"

    win_radius = f"{rounding}px" if rounding > 0 else "0px"

    return f"""
    window {{
        background-color: {bg};
        border: 2px solid {inactive_border};
        border-radius: {win_radius};
    }}

    window:focus-within {{
        border-color: {active_border};
    }}

    .display-box {{
        padding: 12px 14px;
    }}

    .entry {{
        background: transparent;
        border: none;
        box-shadow: none;
        color: {fg};
        font-size: 22px;
        font-weight: bold;
        font-family: monospace;
        caret-color: {green};
        padding: 0;
        min-height: 0;
    }}

    .result-label {{
        color: {green};
        font-size: 14px;
        font-family: monospace;
        opacity: 1.0;
    }}

    .copy-btn {{
        background: transparent;
        border: none;
        box-shadow: none;
        color: {fg};
        font-size: 18px;
        padding: 0;
        min-height: 24px;
        min-width: 24px;
        opacity: 0.2;
    }}

    .copy-btn:hover {{
        opacity: 1.0;
        color: {green};
    }}
    """


# ---------------------------------------------------------------------------
# Safe math evaluator
# ---------------------------------------------------------------------------

def safe_eval(expr_str):
    """Safely evaluate a basic math expression. Returns (result, error_str)."""
    s = expr_str
    s = s.replace("×", "*").replace("÷", "/").replace("^", "**")
    s = re.sub(r"(\d)x(\d)", r"\1*\2", s)
    s = s.replace(":", "/")

    # implicit multiply: 2( -> 2*(, )( -> )*(
    s = re.sub(r"(\d)\(", r"\1*(", s)
    s = re.sub(r"\)\(", r")*(", s)

    if not s.strip():
        return None, None

    # whitelist characters
    allowed = set("0123456789.+-*/()% \t")
    if not all(c in allowed for c in s):
        return None, None

    try:
        tree = ast.parse(s, mode="eval")
        for node in ast.walk(tree):
            if isinstance(node, (ast.Call, ast.Import, ast.ImportFrom, ast.Attribute)):
                return None, None
        result = eval(compile(tree, "<calc>", "eval"), {"__builtins__": {}})
        if isinstance(result, float) and (math.isinf(result) or math.isnan(result)):
            return None, "Error"
        return result, None
    except ZeroDivisionError:
        return None, "÷ by 0"
    except Exception:
        return None, None


def format_number(n):
    if n is None:
        return ""
    if isinstance(n, float) and n == int(n) and abs(n) < 1e15:
        n = int(n)
    if isinstance(n, int):
        return f"{n:,}"
    return f"{n:,.10f}".rstrip("0").rstrip(".")


# ---------------------------------------------------------------------------
# SNI Tray Icon (StatusNotifierItem via D-Bus)
# ---------------------------------------------------------------------------

SNI_IFACE = "org.kde.StatusNotifierItem"
SNI_PATH = "/StatusNotifierItem"
SNI_BUS_NAME = "org.kde.StatusNotifierItem-omacalc"

DBUSMENU_IFACE = "com.canonical.dbusmenu"
DBUSMENU_PATH = "/MenuBar"


class DBusMenu(dbus.service.Object):
    """Minimal DBusMenu for the tray icon context menu."""

    def __init__(self, bus, toggle_cb, quit_cb):
        super().__init__(bus, DBUSMENU_PATH)
        self._toggle_cb = toggle_cb
        self._quit_cb = quit_cb
        self._revision = 1

    @dbus.service.method(DBUSMENU_IFACE, in_signature="iias", out_signature="u(ia{sv}av)")
    def GetLayout(self, parent_id, recursion_depth, property_names):
        if parent_id == 0:
            children = [
                dbus.Struct((
                    dbus.Int32(2),
                    dbus.Dictionary({"label": dbus.String("Quit"), "visible": dbus.Boolean(True)}, signature="sv"),
                    dbus.Array([], signature="v"),
                ), signature=None),
            ]
            layout = dbus.Struct((
                dbus.Int32(0),
                dbus.Dictionary({"children-display": dbus.String("submenu")}, signature="sv"),
                dbus.Array([dbus.Struct(x, variant_level=1) for x in children], signature="v"),
            ), signature=None)
            return (dbus.UInt32(self._revision), layout)
        return (dbus.UInt32(self._revision), dbus.Struct((
            dbus.Int32(parent_id),
            dbus.Dictionary({}, signature="sv"),
            dbus.Array([], signature="v"),
        ), signature=None))

    @dbus.service.method(DBUSMENU_IFACE, in_signature="isvu", out_signature="")
    def Event(self, item_id, event_type, data, timestamp):
        if item_id == 1:
            GLib.idle_add(self._toggle_cb)
        elif item_id == 2:
            GLib.idle_add(self._quit_cb)

    @dbus.service.method(DBUSMENU_IFACE, in_signature="ia(isvu)", out_signature="ai")
    def EventGroup(self, events_ignored_dummy, events):
        id_errors = []
        for item_id, event_type, data, timestamp in events:
            self.Event(item_id, event_type, data, timestamp)
        return dbus.Array(id_errors, signature="i")

    @dbus.service.method(DBUSMENU_IFACE, in_signature="aias", out_signature="a(ia{sv})")
    def GetGroupProperties(self, ids, property_names):
        result = []
        for item_id in ids:
            if item_id == 0:
                result.append((item_id, {"children-display": "submenu"}))
            elif item_id == 2:
                result.append((item_id, {"label": "Quit", "visible": True}))
        return dbus.Array(result, signature="(ia{sv})")

    @dbus.service.method(DBUSMENU_IFACE, in_signature="ias", out_signature="a{sv}")
    def GetProperty(self, item_id, names):
        return dbus.Dictionary({}, signature="sv")

    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature="ss", out_signature="v")
    def Get(self, interface, prop):
        return self._get_menu_prop(prop)

    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface):
        return {
            "Version": dbus.UInt32(4),
            "TextDirection": dbus.String("ltr"),
            "Status": dbus.String("normal"),
            "IconThemePath": dbus.Array([], signature="s"),
        }

    def _get_menu_prop(self, prop):
        props = {
            "Version": dbus.UInt32(4),
            "TextDirection": dbus.String("ltr"),
            "Status": dbus.String("normal"),
            "IconThemePath": dbus.Array([], signature="s"),
        }
        if prop in props:
            return props[prop]
        raise dbus.exceptions.DBusException(f"Unknown property {prop}")


def make_icon_svg(accent_hex, fg_hex):
    """Create a cute calculator SVG tray icon themed to match Omarchy.

    Clean line-art style: rounded calc body with a little screen and
    a prominent = sign. Uses foreground color for the outline so it
    blends with other Waybar tray icons, accent for the = highlight.
    """
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="24" height="24">
  <!-- calc body -->
  <rect x="3" y="1" width="18" height="22" rx="3" ry="3"
        fill="none" stroke="{fg_hex}" stroke-width="1.6"/>
  <!-- screen -->
  <rect x="5.5" y="3.5" width="13" height="4.5" rx="1.2" ry="1.2"
        fill="{fg_hex}" opacity="0.25"/>
  <!-- button grid dots -->
  <circle cx="7.5"  cy="12" r="1.2" fill="{fg_hex}" opacity="0.55"/>
  <circle cx="12"   cy="12" r="1.2" fill="{fg_hex}" opacity="0.55"/>
  <circle cx="16.5" cy="12" r="1.2" fill="{fg_hex}" opacity="0.55"/>
  <circle cx="7.5"  cy="16" r="1.2" fill="{fg_hex}" opacity="0.55"/>
  <circle cx="12"   cy="16" r="1.2" fill="{fg_hex}" opacity="0.55"/>
  <circle cx="16.5" cy="16" r="1.2" fill="{fg_hex}" opacity="0.55"/>
  <!-- = button highlight -->
  <rect x="10" y="19" width="8.5" height="3" rx="1.5" ry="1.5"
        fill="{accent_hex}" opacity="0.9"/>
  <line x1="11.5" y1="20" x2="17" y2="20"
        stroke="{fg_hex}" stroke-width="0.8" stroke-linecap="round" opacity="0.7"/>
  <line x1="11.5" y1="21.2" x2="17" y2="21.2"
        stroke="{fg_hex}" stroke-width="0.8" stroke-linecap="round" opacity="0.7"/>
  <!-- 0 button -->
  <circle cx="7.5" cy="20.5" r="1.2" fill="{fg_hex}" opacity="0.55"/>
</svg>"""


def make_calculator_icon(accent_hex):
    """Create a 22x22 ARGB32 calculator icon as raw pixel data for SNI."""
    w, h = 22, 22
    r, g, b, _ = hex_to_rgba(accent_hex)
    pixels = bytearray(w * h * 4)

    def set_pixel(x, y, rr, gg, bb, aa=255):
        if 0 <= x < w and 0 <= y < h:
            off = (y * w + x) * 4
            pixels[off:off+4] = bytes([aa, rr, gg, bb])

    def fill_rect(x0, y0, x1, y1, rr, gg, bb, aa=255):
        for yy in range(y0, y1):
            for xx in range(x0, x1):
                set_pixel(xx, yy, rr, gg, bb, aa)

    # Rounded body
    fill_rect(2, 3, 20, 19, r, g, b)
    fill_rect(3, 2, 19, 20, r, g, b)
    set_pixel(2, 2, r, g, b)
    set_pixel(19, 2, r, g, b)
    set_pixel(2, 19, r, g, b)
    set_pixel(19, 19, r, g, b)

    # Screen area (darker)
    fill_rect(4, 4, 18, 9, 0, 0, 0, 120)

    # Button dots
    for bx, by in [(6, 11), (10, 11), (14, 11),
                   (6, 14), (10, 14), (14, 14),
                   (6, 17), (10, 17), (14, 17)]:
        fill_rect(bx, by, bx+2, by+2, 255, 255, 255, 180)

    return bytes(pixels), w, h


def write_icon_files(accent_hex, fg_hex):
    """Write themed SVG icon to XDG runtime dir and hicolor theme."""
    runtime = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
    icon_dir = os.path.join(runtime, "omacalc")
    os.makedirs(icon_dir, exist_ok=True)

    svg_content = make_icon_svg(accent_hex, fg_hex)

    svg_path = os.path.join(icon_dir, "omacalc.svg")
    with open(svg_path, "w") as f:
        f.write(svg_content)

    hicolor = os.path.expanduser("~/.local/share/icons/hicolor/scalable/apps")
    os.makedirs(hicolor, exist_ok=True)
    with open(os.path.join(hicolor, "omacalc.svg"), "w") as f:
        f.write(svg_content)

    return svg_path


class StatusNotifierItem(dbus.service.Object):
    """SNI tray icon implemented via D-Bus."""

    def __init__(self, bus, bus_name, icon_pixmap, toggle_cb, quit_cb):
        super().__init__(bus, SNI_PATH)
        self._bus_name = bus_name
        self._icon_pixmap = icon_pixmap
        self._toggle_cb = toggle_cb
        self._quit_cb = quit_cb
        self._menu = DBusMenu(bus, toggle_cb, quit_cb)

    # --- Methods ---

    @dbus.service.method(SNI_IFACE, in_signature="ii", out_signature="")
    def Activate(self, x, y):
        GLib.idle_add(self._toggle_cb)

    @dbus.service.method(SNI_IFACE, in_signature="ii", out_signature="")
    def SecondaryActivate(self, x, y):
        GLib.idle_add(self._toggle_cb)

    @dbus.service.method(SNI_IFACE, in_signature="ii", out_signature="")
    def ContextMenu(self, x, y):
        pass

    @dbus.service.method(SNI_IFACE, in_signature="is", out_signature="")
    def Scroll(self, delta, orientation):
        pass

    # --- Properties via standard interface ---

    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature="ss", out_signature="v")
    def Get(self, interface, prop):
        if interface == SNI_IFACE:
            return self._get_prop(prop)
        raise dbus.exceptions.DBusException(f"Unknown interface {interface}")

    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface):
        if interface == SNI_IFACE:
            props = {}
            for name in ("Category", "Id", "Title", "Status", "IconName",
                         "IconPixmap", "Menu", "ItemIsMenu",
                         "IconThemePath", "ToolTip"):
                try:
                    props[name] = self._get_prop(name)
                except Exception:
                    pass
            return props
        return {}

    def _get_prop(self, prop):
        if prop == "Category":
            return dbus.String("ApplicationStatus")
        if prop == "Id":
            return dbus.String("omacalc")
        if prop == "Title":
            return dbus.String("OmaCalc")
        if prop == "Status":
            return dbus.String("Active")
        if prop == "IconName":
            return dbus.String("omacalc")
        if prop == "IconPixmap":
            pixels, w, h = self._icon_pixmap
            return dbus.Array([
                dbus.Struct((dbus.Int32(w), dbus.Int32(h), dbus.ByteArray(pixels)), signature=None)
            ], signature="(iiay)")
        if prop == "Menu":
            return dbus.ObjectPath(DBUSMENU_PATH)
        if prop == "ItemIsMenu":
            return dbus.Boolean(False)
        if prop == "IconThemePath":
            return dbus.String(os.path.expanduser("~/.local/share/icons"))
        if prop == "ToolTip":
            return dbus.Struct((
                dbus.String(""),
                dbus.Array([], signature="(iiay)"),
                dbus.String("OmaCalc"),
                dbus.String("Calculator"),
            ), signature=None)
        if prop == "WindowId":
            return dbus.UInt32(0)
        raise dbus.exceptions.DBusException(f"Unknown property {prop}")


def register_sni(bus, bus_name):
    """Register our SNI with the StatusNotifierWatcher."""
    try:
        watcher = bus.get_object(
            "org.kde.StatusNotifierWatcher",
            "/StatusNotifierWatcher",
        )
        watcher.RegisterStatusNotifierItem(
            bus_name,
            dbus_interface="org.kde.StatusNotifierWatcher",
        )
        return True
    except dbus.exceptions.DBusException:
        return False


# ---------------------------------------------------------------------------
# Calculator Window
# ---------------------------------------------------------------------------

class CalcWindow(Gtk.Window):
    def __init__(self, app):
        super().__init__(application=app, title="OmaCalc")
        self.set_default_size(280, -1)
        self.set_resizable(False)

        # Layer shell: anchor top-right, above normal windows
        Gtk4LayerShell.init_for_window(self)
        Gtk4LayerShell.set_layer(self, Gtk4LayerShell.Layer.TOP)
        Gtk4LayerShell.set_anchor(self, Gtk4LayerShell.Edge.TOP, True)
        Gtk4LayerShell.set_anchor(self, Gtk4LayerShell.Edge.RIGHT, True)
        Gtk4LayerShell.set_margin(self, Gtk4LayerShell.Edge.TOP, 10)
        Gtk4LayerShell.set_margin(self, Gtk4LayerShell.Edge.RIGHT, 10)
        Gtk4LayerShell.set_keyboard_mode(self, Gtk4LayerShell.KeyboardMode.ON_DEMAND)
        Gtk4LayerShell.set_namespace(self, "omacalc")

        self._raw_result = None
        self._formatting = False

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        vbox.add_css_class("display-box")
        self.set_child(vbox)

        # Input row: copy button (left) + entry (right)
        input_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        vbox.append(input_box)

        self.copy_btn = Gtk.Button(label="\u29C9")  # ⧉ copy symbol
        self.copy_btn.add_css_class("copy-btn")
        self.copy_btn.set_valign(Gtk.Align.CENTER)
        self.copy_btn.connect("clicked", self._on_copy)
        input_box.append(self.copy_btn)

        self.entry = Gtk.Entry()
        self.entry.set_placeholder_text("0")
        self.entry.add_css_class("entry")
        self.entry.set_alignment(1.0)  # right-align
        self.entry.set_hexpand(True)
        self.entry.connect("changed", self._on_changed)
        self.entry.connect("activate", self._on_activate)
        input_box.append(self.entry)

        # Live result label
        self.result_label = Gtk.Label(label="")
        self.result_label.set_halign(Gtk.Align.END)
        self.result_label.set_hexpand(True)
        self.result_label.add_css_class("result-label")
        vbox.append(self.result_label)

        # Keyboard shortcuts
        key_ctrl = Gtk.EventControllerKey()
        key_ctrl.connect("key-pressed", self._on_key)
        self.add_controller(key_ctrl)

    def _on_changed(self, entry):
        """Live eval on every keystroke, with comma formatting in the input."""
        if self._formatting:
            return
        text = entry.get_text()
        raw = text.replace(",", "")

        # Defer the set_text to avoid GTK's "irreversible action" warning
        formatted = self._add_commas(raw)
        if formatted != text:
            old_pos = entry.get_position()
            raw_pos = old_pos - text[:old_pos].count(",")
            GLib.idle_add(self._apply_format, formatted, raw_pos)

        result, error = safe_eval(raw)
        if error:
            self.result_label.set_text(error)
            self._raw_result = None
        elif result is not None:
            self.result_label.set_text(format_number(result))
            self._raw_result = str(result) if isinstance(result, int) else str(result)
        else:
            self.result_label.set_text("")
            self._raw_result = None

    def _apply_format(self, formatted, raw_pos):
        """Deferred entry update to avoid GTK re-entrancy warnings."""
        self._formatting = True
        self.entry.set_text(formatted)
        # Map raw cursor position into formatted string
        new_pos = 0
        seen = 0
        for ch in formatted:
            if seen == raw_pos:
                break
            new_pos += 1
            if ch != ",":
                seen += 1
        else:
            new_pos = len(formatted)
        self.entry.set_position(new_pos)
        self._formatting = False
        return False  # don't repeat

    @staticmethod
    def _add_commas(text):
        """Add thousand separators to number sequences in an expression."""
        def _fmt(m):
            s = m.group()
            if "." in s:
                integer, dec = s.split(".", 1)
                return f"{int(integer):,}.{dec}" if integer else s
            return f"{int(s):,}"
        return re.sub(r'\d+\.?\d*', _fmt, text)

    def _on_copy(self, button):
        """Copy result to clipboard."""
        if self._raw_result:
            clipboard = self.get_clipboard()
            clipboard.set(self._raw_result)
            self.copy_btn.set_label("\u2713")  # ✓
            GLib.timeout_add(800, lambda: self.copy_btn.set_label("\u29C9") or False)
        self.entry.grab_focus()

    def _on_activate(self, entry):
        """Fired by Gtk.Entry on Enter/Return — promotes result into input."""
        if self._raw_result:
            clipboard = self.get_clipboard()
            clipboard.set(self._raw_result)
            self.entry.set_text(self._raw_result)
            self.entry.set_position(-1)  # cursor at end
            self.result_label.set_text("")

    def _on_key(self, _ctrl, keyval, _keycode, state):
        key = Gdk.keyval_name(keyval)

        if key == "Escape":
            self.set_visible(False)
            return True

        return False

    def focus_entry(self):
        """Focus the entry when window is shown."""
        self.entry.grab_focus()


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

class OmaCalcApp(Gtk.Application):
    def __init__(self, session_bus, bus_name_obj, sni):
        super().__init__(
            application_id="com.omarchy.omacalc",
            flags=Gio.ApplicationFlags.FLAGS_NONE,
        )
        self.win = None
        self._session_bus = session_bus
        self._bus_name_obj = bus_name_obj
        self.sni = sni
        self._last_toggle = 0

    def _init_window(self):
        colors = load_theme_colors()
        rounding = get_hyprland_rounding()
        css_text = build_css(colors, rounding)
        self._css_provider = Gtk.CssProvider()
        self._css_provider.load_from_string(css_text)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            self._css_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
        self.win = CalcWindow(self)
        self.sni._toggle_cb = self.toggle_window
        self.sni._quit_cb = self.quit_app
        self.sni._menu._toggle_cb = self.toggle_window
        self.sni._menu._quit_cb = self.quit_app

        # Watch the current/ directory — theme switches rm+mv the theme/ dir,
        # so monitoring colors.toml directly misses the swap.
        current_dir = os.path.expanduser("~/.config/omarchy/current")
        gdir = Gio.File.new_for_path(current_dir)
        self._theme_monitor = gdir.monitor_directory(
            Gio.FileMonitorFlags.NONE, None
        )
        self._theme_monitor.connect("changed", self._on_theme_changed)

    def _on_theme_changed(self, monitor, file, other_file, event_type):
        """Debounce theme dir changes — reload 100ms after last event."""
        if event_type not in (
            Gio.FileMonitorEvent.CHANGED,
            Gio.FileMonitorEvent.CREATED,
            Gio.FileMonitorEvent.MOVED_IN,
            Gio.FileMonitorEvent.CHANGES_DONE_HINT,
        ):
            return
        # Only react to the theme dir itself being replaced
        if file and "theme" not in file.get_basename():
            return
        if hasattr(self, '_theme_reload_id') and self._theme_reload_id:
            GLib.source_remove(self._theme_reload_id)
        self._theme_reload_id = GLib.timeout_add(100, self._reload_theme)

    def _reload_theme(self):
        """Actually reload CSS and tray icon."""
        self._theme_reload_id = None
        colors = load_theme_colors()
        rounding = get_hyprland_rounding()
        css_text = build_css(colors, rounding)
        self._css_provider.load_from_string(css_text)

        # Update tray icon
        accent = colors.get("accent", "#7aa2f7")
        fg = colors.get("foreground", "#a9b1d6")
        write_icon_files(accent, fg)
        self.sni._icon_pixmap = make_calculator_icon(accent)
        return False  # don't repeat

    def do_activate(self):
        if not self.win:
            self._init_window()
            self.toggle_window()

    def toggle_window(self):
        now = GLib.get_monotonic_time()
        if now - self._last_toggle < 300_000:  # 300ms debounce
            return
        self._last_toggle = now
        if self.win:
            if self.win.get_visible():
                self.win.set_visible(False)
            else:
                self.win.set_visible(True)
                self.win.present()
                self.win.focus_entry()

    def quit_app(self):
        sys.exit(0)


def ensure_layer_shell_preload():
    """Re-exec with LD_PRELOAD if gtk4-layer-shell isn't preloaded."""
    lib = "/usr/lib/libgtk4-layer-shell.so"
    if not os.path.exists(lib):
        return
    preload = os.environ.get("LD_PRELOAD", "")
    if "libgtk4-layer-shell" in preload:
        return
    os.environ["LD_PRELOAD"] = f"{lib}:{preload}" if preload else lib
    os.execv("/usr/bin/python3", ["python3"] + [os.path.abspath(__file__)])


def install_autostart():
    """Create a .desktop file in ~/.config/autostart/ to start on login."""
    autostart_dir = os.path.expanduser("~/.config/autostart")
    os.makedirs(autostart_dir, exist_ok=True)
    script_path = os.path.abspath(__file__)
    desktop = f"""[Desktop Entry]
Name=OmaCalc
Comment=Tray calculator for Omarchy
Exec={script_path}
StartupNotify=false
Terminal=false
Type=Application
"""
    dest = os.path.join(autostart_dir, "omacalc.desktop")
    with open(dest, "w") as f:
        f.write(desktop)
    print(f"Installed autostart entry: {dest}")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--install":
        install_autostart()
        return

    ensure_layer_shell_preload()
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    # Private dbus connection for SNI (avoids conflict with GApplication's GDBus)
    session_bus = dbus.SessionBus(private=True)
    bus_name_obj = dbus.service.BusName(SNI_BUS_NAME, session_bus)

    colors = load_theme_colors()
    accent = colors.get("accent", "#7aa2f7")
    icon_fg = colors.get("foreground", "#a9b1d6")
    icon_pixmap = make_calculator_icon(accent)
    write_icon_files(accent, icon_fg)

    # Placeholder callbacks — replaced once app window exists
    sni = StatusNotifierItem(
        session_bus, SNI_BUS_NAME, icon_pixmap,
        toggle_cb=lambda: None,
        quit_cb=lambda: None,
    )

    register_sni(session_bus, SNI_BUS_NAME)

    app = OmaCalcApp(session_bus, bus_name_obj, sni)
    app.run(None)


if __name__ == "__main__":
    main()
