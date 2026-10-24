#!/bin/bash
# Roll back to stock tiny-dfr.
set -e
systemctl --user disable --now omarchy-touchbar.service || true
sudo systemctl unmask tiny-dfr.service
sudo systemctl start tiny-dfr.service
sudo rm -f /etc/udev/rules.d/99-omarchy-touchbar-uinput.rules \
  /etc/modules-load.d/uinput.conf \
  /etc/tmpfiles.d/zz-omarchy-uinput.conf
sudo udevadm control --reload-rules
echo "==> tiny-dfr restored"
