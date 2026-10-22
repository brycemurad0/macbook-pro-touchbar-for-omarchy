#!/bin/bash
# Install omarchy-touchbar: files, systemd units, uinput access, and
# reversible replacement of stock tiny-dfr.
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/.config/omarchy/touchbar"

echo "==> install files to $DEST"
install -Dm755 "$DIR/touchbar.py" "$DEST/touchbar.py"
install -Dm755 "$DIR/pipecat_voice.py" "$DEST/pipecat_voice.py"
install -Dm755 "$DIR/voice_tools.py" "$DEST/voice_tools.py"
install -Dm755 "$DIR/restore.sh" "$DEST/restore.sh"
install -Dm644 "$DIR/systemd/omarchy-touchbar.service" \
  "$HOME/.config/systemd/user/omarchy-touchbar.service"
install -Dm644 "$DIR/systemd/omarchy-voice.service" \
  "$HOME/.config/systemd/user/omarchy-voice.service"
[ -f "$DEST/voice.json" ] || \
  install -Dm644 "$DIR/voice.example.json" "$DEST/voice.json"
[ -f "$DEST/fleet.json" ] || \
  install -Dm644 "$DIR/fleet.example.json" "$DEST/fleet.json"

echo "==> voice venv (Pipecat + local STT/VAD; system python stays clean)"
if [ ! -x "$DEST/.venv/bin/python" ]; then
  python3 -m venv "$DEST/.venv"
  "$DEST/.venv/bin/pip" install -q --upgrade pip
  "$DEST/.venv/bin/pip" install -q \
    'pipecat-ai[silero,moonshine,elevenlabs,google,openai,anthropic]'
fi

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
echo "    Voice: put ELEVENLABS_API_KEY + GEMINI_API_KEY in"
echo "    $DEST/voice-secrets.env (chmod 600), then toggle voice on FLOW."
echo "    Roll back any time:  $DEST/restore.sh"
