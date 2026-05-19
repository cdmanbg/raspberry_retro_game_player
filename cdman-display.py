#!/usr/bin/env python3
"""
cdman-display - drives the Pirate Audio ST7789 LCD.

Shows:
  - device IP address(es)
  - currently playing song (read from status.json)
  - idle indicator when nothing is playing

Reads /<script_dir>/status.json which the web server writes.
Status file format:  {"playing": "song1.txt" | null, "tempo": 220, "volume": 0.01}

Run:  python3 cdman-display.py
"""
import os
import sys
import json
import time
import socket
import subprocess

from PIL import Image, ImageDraw, ImageFont
import ST7789

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
STATUS_FILE = os.environ.get("CDMAN_STATUS_FILE") or os.path.join(SCRIPT_DIR, "status.json")
REFRESH_SEC = 1.0           # how often to repaint when nothing changed
IP_REFRESH_SEC = 10.0       # re-query IP periodically (in case of DHCP changes)

# ---- display setup (Pirate Audio pinout) ----
disp = ST7789.ST7789(
    rotation=90,
    port=0,
    cs=ST7789.BG_SPI_CS_FRONT,   # BCM 7 on Pirate Audio
    dc=9,
    backlight=13,
    spi_speed_hz=80_000_000,
)
W, H = disp.width, disp.height   # 240 x 240


# ---- fonts ----
def load_font(size):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for p in candidates:
        if os.path.isfile(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()

font_title = load_font(18)
font_big   = load_font(22)
font_mid   = load_font(16)
font_small = load_font(13)


# ---- helpers ----
def get_ips():
    """Return list of non-loopback IPv4 addresses."""
    ips = []
    try:
        out = subprocess.run(
            ["hostname", "-I"], capture_output=True, text=True, timeout=2
        ).stdout.strip()
        for tok in out.split():
            if "." in tok and not tok.startswith("127."):
                ips.append(tok)
    except Exception:
        pass
    # Fallback: connect a UDP socket to discover the route-out IP
    if not ips:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ips.append(s.getsockname()[0])
            s.close()
        except Exception:
            pass
    return ips


def read_status():
    try:
        with open(STATUS_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"playing": None}


def text_size(draw, text, font):
    # Pillow >=8 has textbbox; older has textsize
    if hasattr(draw, "textbbox"):
        l, t, r, b = draw.textbbox((0, 0), text, font=font)
        return r - l, b - t
    return draw.textsize(text, font=font)


def fit_text(draw, text, font, max_w):
    """Truncate text with an ellipsis so it fits in max_w."""
    w, _ = text_size(draw, text, font)
    if w <= max_w:
        return text
    ell = "…"
    while text and text_size(draw, text + ell, font)[0] > max_w:
        text = text[:-1]
    return text + ell


def wrap_text(draw, text, font, max_w, max_lines=2):
    """Naive word/char wrap into up to max_lines lines, ellipsizing the last."""
    words = text.split()
    lines, cur = [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if text_size(draw, trial, font)[0] <= max_w:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w
            if len(lines) >= max_lines:
                break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    if len(lines) == max_lines:
        lines[-1] = fit_text(draw, lines[-1], font, max_w)
    return lines


# ---- frame builder ----
PHASE = 0  # for animated "playing" dots

def draw_frame(playing, ips, tempo=None, volume=None):
    global PHASE
    img = Image.new("RGB", (W, H), (12, 15, 20))
    d = ImageDraw.Draw(img)

    # Title bar
    d.rectangle((0, 0, W, 30), fill=(15, 30, 60))
    d.text((10, 5), "♪ cdman", font=font_title, fill=(124, 209, 255))

    # IP block
    y = 40
    d.text((10, y), "IP", font=font_small, fill=(124, 209, 255))
    y += 16
    if ips:
        for ip in ips[:2]:
            line = fit_text(d, ip, font_mid, W - 20)
            d.text((10, y), line, font=font_mid, fill=(217, 226, 240))
            y += 20
    else:
        d.text((10, y), "(no network)", font=font_mid, fill=(200, 100, 100))
        y += 20

    # Divider
    y += 8
    d.line((10, y, W - 10, y), fill=(40, 50, 74))
    y += 8

    # Now playing
    d.text((10, y), "NOW PLAYING", font=font_small, fill=(124, 209, 255))
    y += 18
    if playing:
        # strip extension for niceness
        name = os.path.splitext(playing)[0]
        lines = wrap_text(d, name, font_big, W - 20, max_lines=2)
        for ln in lines:
            d.text((10, y), ln, font=font_big, fill=(255, 255, 255))
            y += 26
        # animated dots
        dots = "." * (PHASE % 4)
        d.text((10, y), f"playing{dots}", font=font_small, fill=(110, 224, 110))
    else:
        d.text((10, y), "— idle —", font=font_big, fill=(110, 130, 160))
        y += 28

    # Footer: tempo / volume
    if tempo is not None or volume is not None:
        foot_y = H - 18
        parts = []
        if tempo  is not None: parts.append(f"T:{int(tempo)}")
        if volume is not None: parts.append(f"V:{float(volume):.3f}")
        d.text((10, foot_y), "  ".join(parts), font=font_small, fill=(155, 170, 200))

    # LED in title bar
    led_color = (110, 224, 110) if playing else (52, 64, 85)
    d.ellipse((W - 22, 9, W - 10, 21), fill=led_color)

    disp.display(img)
    PHASE += 1


def main():
    disp.set_backlight(1)
    last_ips_check = 0
    ips = get_ips()
    last_state = None

    print("cdman-display running")
    try:
        while True:
            now = time.time()
            if now - last_ips_check > IP_REFRESH_SEC:
                ips = get_ips()
                last_ips_check = now

            status = read_status()
            playing = status.get("playing")
            tempo   = status.get("tempo")
            volume  = status.get("volume")

            state_key = (tuple(ips), playing, tempo, volume, PHASE if playing else 0)
            # repaint always (so animated dots tick); cheap at 1 Hz
            draw_frame(playing, ips, tempo, volume)
            last_state = state_key

            time.sleep(REFRESH_SEC)
    except KeyboardInterrupt:
        pass
    finally:
        # Optional: leave the screen on with a goodbye, or just clear.
        try:
            img = Image.new("RGB", (W, H), (0, 0, 0))
            disp.display(img)
        except Exception:
            pass


if __name__ == "__main__":
    main()
