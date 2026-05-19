## Installation

Tested on **Raspberry Pi OS Lite (Bookworm, 64-bit)** on a Pi Zero 2 W with a
Pirate Audio 3W Stereo Amp HAT.

### Quick install (recommended)

After flashing Pi OS Lite (with SSH and Wi-Fi pre-configured via Raspberry Pi
Imager), SSH in and run:

```bash
sudo apt update && sudo apt install -y git
sudo git clone https://github.com/cdmanbg/raspberry_retro_game_player.git /srv/rpi_retro_game_player
sudo /srv/rpi_retro_game_player/install.sh
```

If the installer says a reboot is needed (it changed boot config for the
Pirate Audio DAC), reboot and run it again:

```bash
sudo reboot
# after reboot:
sudo /srv/rpi_retro_game_player/install.sh
```

When it finishes, open `http://<pi-ip>:8080` (or `http://<hostname>.local:8080`)
in a browser.

### What the installer does

- Installs system packages (`python3-flask`, `python3-pygame`, `python3-pil`,
  `python3-spidev`, `fonts-dejavu-core`, `avahi-daemon`, …) via `apt`
- Installs the `st7789` Python package (the only thing not in apt) via pip
- Enables I²S audio + SPI in `/boot/firmware/config.txt`, disables the on-board
  audio that would otherwise compete with the Pirate Audio DAC
- Clones the repo to `/srv/rpi_retro_game_player` (code, read-only)
- Creates `/var/lib/cdman/songs/` for user-uploaded songs (writable, survives
  `git pull`) and seeds it with the example songs on first install
- Adds the user to the `audio`, `spi`, `gpio` groups
- Installs `cdman-web.service` and `cdman-display.service` systemd units,
  enables them, and starts them

The installer is **idempotent** — run it again any time to apply updates or
recover a broken install. It will never overwrite a song file that already
exists in `/var/lib/cdman/songs/`.

### File layout

| Path | What |
|---|---|
| `/srv/rpi_retro_game_player/` | git checkout, owned by root, read-only to services |
| `/var/lib/cdman/songs/` | user-writable songs directory |
| `/var/lib/cdman/status.json` | runtime state read by the display daemon |
| `/etc/systemd/system/cdman-{web,display}.service` | systemd units |

### Updating

```bash
cd /srv/rpi_retro_game_player
sudo git pull
sudo systemctl restart cdman-web cdman-display
```

Or just re-run the installer — same effect, and it'll also pick up any new
system-package dependencies.

### Troubleshooting

```bash
# Service status and logs
systemctl status cdman-web cdman-display
journalctl -u cdman-web -f
journalctl -u cdman-display -f

# Audio device detected?
aplay -l

# SPI device present (for the display)?
ls /dev/spi*

# Manual test of each piece
python3 /srv/rpi_retro_game_player/cdman-player <songname>
python3 /srv/rpi_retro_game_player/cdman-web.py
python3 /srv/rpi_retro_game_player/cdman-display.py
```
