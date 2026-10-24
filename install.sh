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

echo "==> uinput access (survives reboot; static node is otherwise 0600 root:root)"
echo 'KERNEL=="uinput", GROUP="input", MODE="0660", TAG+="uaccess"' \
  | sudo tee /etc/udev/rules.d/99-omarchy-touchbar-uinput.rules >/dev/null
sudo udevadm control --reload-rules
# kmod's static node is created 0600 before udev ever sees a device, so a
# user open cannot autoload the module and the udev rule never runs.
echo uinput | sudo tee /etc/modules-load.d/uinput.conf >/dev/null
printf '%s\n' '# Override /run/tmpfiles.d/static-nodes.conf (0600 root:root).' \
  'c /dev/uinput 0660 root input - 10:223' \
  | sudo tee /etc/tmpfiles.d/zz-omarchy-uinput.conf >/dev/null
sudo modprobe uinput 2>/dev/null || true
sudo systemd-tmpfiles --create /etc/tmpfiles.d/zz-omarchy-uinput.conf

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
