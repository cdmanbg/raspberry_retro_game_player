#!/usr/bin/env bash
# cdman / raspberry_retro_game_player installer for Raspberry Pi OS Lite.
#
# Designed for a fresh Pi OS Lite (Bookworm) install. Idempotent: safe to
# run again after a reboot or to update an existing deployment.
#
# Layout produced:
#   /srv/rpi_retro_game_player/         <- git checkout (read-only by default)
#   /var/lib/cdman/songs/               <- user-uploaded songs (writable)
#   /var/lib/cdman/status.json          <- runtime state for display
#   /etc/systemd/system/cdman-{web,display}.service
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/cdmanbg/raspberry_retro_game_player/main/install.sh | sudo bash
# or:
#   git clone https://github.com/cdmanbg/raspberry_retro_game_player /srv/rpi_retro_game_player
#   sudo /srv/rpi_retro_game_player/install.sh
#
# Run as a regular user that can sudo, or directly as root.

set -euo pipefail

# ============================================================
# Config (override via env if needed)
# ============================================================
REPO_URL="${REPO_URL:-https://github.com/cdmanbg/raspberry_retro_game_player.git}"
APP_DIR="${APP_DIR:-/srv/rpi_retro_game_player}"
DATA_DIR="${DATA_DIR:-/var/lib/cdman}"
SONGS_DIR="$DATA_DIR/songs"
STATUS_FILE="$DATA_DIR/status.json"

# Pick the unprivileged user that will own files and run the services.
if [[ -n "${CDMAN_USER:-}" ]]; then
    RUN_USER="$CDMAN_USER"
elif [[ -n "${SUDO_USER:-}" && "$SUDO_USER" != "root" ]]; then
    RUN_USER="$SUDO_USER"
else
    # Pick the first regular user (UID >= 1000)
    RUN_USER="$(awk -F: '$3>=1000 && $3<65000 {print $1; exit}' /etc/passwd)"
fi
[[ -n "$RUN_USER" ]] || { echo "Cannot determine run user. Set CDMAN_USER." >&2; exit 1; }

# Sudo helper - lets the script work whether invoked as root or as a sudo user
if [[ $EUID -eq 0 ]]; then SUDO=""; else SUDO="sudo"; fi

# ============================================================
# Pretty output
# ============================================================
COL_INFO="\033[1;36m"; COL_OK="\033[1;32m"; COL_WARN="\033[1;33m"; COL_ERR="\033[1;31m"; COL_OFF="\033[0m"
step()  { printf "\n${COL_INFO}== %s ==${COL_OFF}\n" "$*"; }
ok()    { printf "${COL_OK}  ok${COL_OFF}    %s\n"   "$*"; }
note()  { printf "${COL_INFO}  ..${COL_OFF}    %s\n"   "$*"; }
warn()  { printf "${COL_WARN}  !!${COL_OFF}    %s\n"   "$*"; }
fail()  { printf "${COL_ERR}  ERR${COL_OFF}   %s\n"   "$*" >&2; exit 1; }

# ============================================================
# Sanity
# ============================================================
step "Sanity checks"
if [[ ! -f /etc/os-release ]] || ! grep -qi 'raspberry\|debian' /etc/os-release; then
    warn "/etc/os-release doesn't look like Pi OS or Debian. Continuing anyway."
else
    ok "Detected $(. /etc/os-release; echo "$PRETTY_NAME")"
fi
ok "Run user: $RUN_USER"
ok "App dir:  $APP_DIR"
ok "Data dir: $DATA_DIR"

id "$RUN_USER" >/dev/null 2>&1 || fail "User '$RUN_USER' does not exist."

# Detect boot config path (Bookworm uses /boot/firmware; older uses /boot)
if   [[ -f /boot/firmware/config.txt ]]; then BOOT_CFG=/boot/firmware/config.txt
elif [[ -f /boot/config.txt           ]]; then BOOT_CFG=/boot/config.txt
else fail "Could not find Pi boot config.txt (looked in /boot/firmware and /boot)"
fi
ok "Boot config: $BOOT_CFG"

# ============================================================
# 1) System packages
# ============================================================
step "Installing system packages"
$SUDO apt-get update -qq
$SUDO apt-get install -y --no-install-recommends \
    git \
    python3 python3-pip \
    python3-numpy python3-pygame python3-flask python3-pil \
    python3-spidev python3-rpi.gpio \
    fonts-dejavu-core \
    avahi-daemon \
    alsa-utils
ok "Apt packages installed"

# st7789 isn't in apt; install via pip with --break-system-packages (Bookworm requirement)
if ! python3 -c "import ST7789" 2>/dev/null; then
    note "Installing st7789 via pip"
    $SUDO pip3 install --break-system-packages st7789 >/dev/null
    ok "st7789 installed"
else
    ok "st7789 already installed"
fi

# ============================================================
# 2) Boot config for Pirate Audio (I²S DAC + SPI display)
# ============================================================
step "Configuring Pirate Audio in $BOOT_CFG"
REBOOT_NEEDED=0

# Comment out the line that enables the on-board audio so it doesn't grab the
# default sound device away from the HifiBerry DAC.
if $SUDO grep -qE '^[[:space:]]*dtparam=audio=on' "$BOOT_CFG"; then
    note "Disabling default on-board audio (dtparam=audio=on)"
    $SUDO sed -i 's/^[[:space:]]*dtparam=audio=on/#dtparam=audio=on  # cdman: use Pirate Audio instead/' "$BOOT_CFG"
    REBOOT_NEEDED=1
fi

# Append the Pirate Audio block if it isn't already there
if ! $SUDO grep -q '^# --- cdman / Pirate Audio ---' "$BOOT_CFG"; then
    note "Appending Pirate Audio settings"
    $SUDO tee -a "$BOOT_CFG" >/dev/null <<'EOF'

# --- cdman / Pirate Audio ---
dtparam=i2s=on
dtoverlay=hifiberry-dac
gpio=25=op,dh
dtparam=spi=on
EOF
    REBOOT_NEEDED=1
else
    ok "Pirate Audio block already present"
fi

# ============================================================
# 3) Code: clone or update the repo at /srv/rpi_retro_game_player
# ============================================================
step "Installing application at $APP_DIR"
if [[ -d "$APP_DIR/.git" ]]; then
    note "Repo exists - pulling latest"
    $SUDO git -C "$APP_DIR" fetch --quiet origin
    $SUDO git -C "$APP_DIR" reset --hard --quiet origin/HEAD
    ok "Updated $APP_DIR"
elif [[ -d "$APP_DIR" && ! -z "$(ls -A "$APP_DIR" 2>/dev/null)" ]]; then
    warn "$APP_DIR exists but is not a git repo. Leaving alone."
    warn "If you want a fresh clone: sudo rm -rf $APP_DIR  and re-run."
else
    $SUDO mkdir -p "$APP_DIR"
    note "Cloning $REPO_URL"
    $SUDO git clone --quiet "$REPO_URL" "$APP_DIR"
    ok "Cloned to $APP_DIR"
fi

# Ownership: code owned by root (so a service user can't chmod it away),
# but readable by everyone. The data dir is writable by RUN_USER.
$SUDO chown -R root:root "$APP_DIR"
$SUDO find "$APP_DIR" -type d -exec chmod 755 {} \;
$SUDO find "$APP_DIR" -type f -exec chmod 644 {} \;
# Executables
for f in cdman-player cdman-convert install.sh; do
    [[ -f "$APP_DIR/$f" ]] && $SUDO chmod 755 "$APP_DIR/$f"
done
ok "Set permissions on $APP_DIR"

# ============================================================
# 4) Data directory (user-writable)
# ============================================================
step "Setting up data directory at $DATA_DIR"
$SUDO mkdir -p "$SONGS_DIR"
$SUDO chown -R "$RUN_USER":"$RUN_USER" "$DATA_DIR"
$SUDO chmod 755 "$DATA_DIR" "$SONGS_DIR"

# Seed example songs on first install only - never overwrite user files
if [[ -d "$APP_DIR/songs" ]]; then
    shopt -s nullglob
    new_files=0
    for src in "$APP_DIR/songs"/*.txt "$APP_DIR/songs"/*.json; do
        dst="$SONGS_DIR/$(basename "$src")"
        if [[ ! -e "$dst" ]]; then
            $SUDO install -o "$RUN_USER" -g "$RUN_USER" -m 644 "$src" "$dst"
            new_files=$((new_files+1))
        fi
    done
    shopt -u nullglob
    ok "Seeded $new_files example song(s) into $SONGS_DIR (existing files left alone)"
fi

# ============================================================
# 5) Audio group membership for the run user
# ============================================================
step "Audio group membership"
if id -nG "$RUN_USER" | tr ' ' '\n' | grep -qx audio; then
    ok "$RUN_USER is already in the audio group"
else
    $SUDO usermod -aG audio "$RUN_USER"
    ok "Added $RUN_USER to audio group (takes effect on next login or service restart)"
fi
# spi/gpio groups for display access; ignore if they don't exist on this system
for g in spi gpio; do
    if getent group "$g" >/dev/null && ! id -nG "$RUN_USER" | tr ' ' '\n' | grep -qx "$g"; then
        $SUDO usermod -aG "$g" "$RUN_USER"
        ok "Added $RUN_USER to $g group"
    fi
done

# ============================================================
# 6) Install systemd unit files from docs/ templates
# ============================================================
step "Installing systemd services"
for svc in cdman-web cdman-display; do
    src="$APP_DIR/docs/$svc.service"
    [[ -f "$src" ]] || fail "Missing template: $src"
    tmp="$(mktemp)"
    sed -e "s|__USER__|$RUN_USER|g" -e "s|__APP_DIR__|$APP_DIR|g" "$src" >"$tmp"
    $SUDO install -m 644 "$tmp" "/etc/systemd/system/$svc.service"
    rm -f "$tmp"
    ok "/etc/systemd/system/$svc.service"
done
$SUDO systemctl daemon-reload
ok "Reloaded systemd"

# ============================================================
# 7) Reboot if hardware config changed
# ============================================================
if [[ $REBOOT_NEEDED -eq 1 ]]; then
    step "Reboot required"
    warn "Boot config was changed. Reboot to load the Pirate Audio drivers,"
    warn "then run this installer again to start the services."
    echo
    echo "    sudo reboot"
    echo
    exit 0
fi

# ============================================================
# 8) Hardware sanity (post-reboot)
# ============================================================
step "Hardware sanity checks"
if aplay -l 2>/dev/null | grep -qiE 'hifiberry|i2s'; then
    ok "I²S DAC detected (Pirate Audio)"
else
    warn "No HifiBerry/I²S audio card found. 'aplay -l' shows:"
    aplay -l 2>&1 | sed 's/^/      /' | head -10
    warn "If you just changed boot config, run 'sudo reboot' first."
fi

if [[ -e /dev/spidev0.0 ]]; then
    ok "SPI device /dev/spidev0.0 present"
else
    warn "/dev/spidev0.0 not present. The display won't work."
    warn "Try: sudo raspi-config nonint do_spi 0  &&  sudo reboot"
fi

# ============================================================
# 9) Start services
# ============================================================
step "Starting services"
$SUDO systemctl enable --now cdman-web.service cdman-display.service >/dev/null
sleep 2

for svc in cdman-web cdman-display; do
    if systemctl is-active --quiet "$svc"; then
        ok "$svc is active"
    else
        warn "$svc failed to start. Last log lines:"
        $SUDO journalctl -u "$svc" -n 15 --no-pager | sed 's/^/      /'
    fi
done

# ============================================================
# 10) Summary
# ============================================================
IP="$(hostname -I | awk '{print $1}')"
HOST="$(hostname)"

step "Done"
cat <<EOF

  Web UI:    http://$IP:8080
             http://$HOST.local:8080   (from any mDNS-capable device)

  Data:      $SONGS_DIR   (drop .txt / .json songs here)
             $STATUS_FILE (runtime - don't edit)

  Update:    cd $APP_DIR && sudo git pull
             sudo systemctl restart cdman-web cdman-display

  Logs:      journalctl -u cdman-web    -f
             journalctl -u cdman-display -f

  Or just run this installer again - it's idempotent.

EOF
