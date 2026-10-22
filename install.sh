#!/bin/bash
# Install omarchy-touchbar: files, systemd units, uinput access, and
# reversible replacement of stock tiny-dfr.
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/.config/omarchy/touchbar"

echo "==> install files to $DEST"
install -Dm755 "$DIR/touchbar.py" "$DEST/touchbar.py"
install -Dm755 "$DIR/voice_agent.py" "$DEST/voice_agent.py"
install -Dm755 "$DIR/restore.sh" "$DEST/restore.sh"
install -Dm644 "$DIR/systemd/omarchy-touchbar.service" \
  "$HOME/.config/systemd/user/omarchy-touchbar.service"
install -Dm644 "$DIR/systemd/omarchy-voice.service" \
  "$HOME/.config/systemd/user/omarchy-voice.service"
[ -f "$DEST/voice-config.json" ] || \
  install -Dm644 "$DIR/voice-config.example.json" "$DEST/voice-config.json"

echo "==> uinput udev rule (key injection needs the input group device)"
echo 'KERNEL=="uinput", GROUP="input", MODE="0660", TAG+="uaccess"' \
  | sudo tee /etc/udev/rules.d/99-omarchy-touchbar-uinput.rules >/dev/null
sudo udevadm control --reload-rules
sudo chmod 660 /dev/uinput 2>/dev/null || true
sudo chgrp input /dev/uinput 2>/dev/null || true

echo "==> stop + mask tiny-dfr (reversible via restore.sh)"
sudo systemctl stop tiny-dfr.service || true
sudo systemctl mask tiny-dfr.service

echo "==> enable omarchy-touchbar user service"
systemctl --user daemon-reload
systemctl --user enable --now omarchy-touchbar.service

echo "==> done. Live preview: ~/.local/state/omarchy-touchbar/preview.png"
echo "    Roll back any time:  $DEST/restore.sh"
