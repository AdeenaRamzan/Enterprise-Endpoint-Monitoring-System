"""
Persistent tray icon via pystray. Right-click menu: "Monitoring active"
(non-clickable status line), link to the policy text, "Contact IT".
This never hides itself and never runs invisibly -- that's the whole
point of the consent-based design.

pystray works cross-platform (it has a Linux/GTK backend), so this one
CAN be sanity-checked in this sandbox for import/construction, though
actually clicking a real tray icon obviously isn't verifiable headlessly.
"""

import threading
import webbrowser
from pathlib import Path

import pystray
from PIL import Image, ImageDraw


def _make_icon_image():
    # Simple placeholder icon: solid circle. Swap for a real company
    # icon asset before shipping.
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((8, 8, 56, 56), fill=(46, 125, 50, 255))
    return img


def _open_policy(cfg):
    policy_path = Path(cfg["policy_text_path"]).resolve()
    webbrowser.open(f"file://{policy_path}")


def build_tray_icon(cfg):
    menu = pystray.Menu(
        pystray.MenuItem("Monitoring active", lambda: None, enabled=False),
        pystray.MenuItem("View company monitoring policy", lambda: _open_policy(cfg)),
        pystray.MenuItem("Contact IT", lambda: webbrowser.open("mailto:it-support@example.com")),
    )
    icon = pystray.Icon("monitoring_agent", _make_icon_image(), "Monitoring Agent - Active", menu)
    return icon


def start_tray_icon_thread(cfg):
    icon = build_tray_icon(cfg)
    threading.Thread(target=icon.run, daemon=True).start()
    return icon
