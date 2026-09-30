#!/usr/bin/env bash
set -euo pipefail

echo "== WFAS Native UDP Client kurulumu =="

if ! command -v aplay >/dev/null 2>&1; then
    echo "-> alsa-utils kuruluyor (aplay için)..."
    sudo apt-get update
    sudo apt-get install -y alsa-utils
else
    echo "-> aplay zaten kurulu."
fi

if command -v pactl >/dev/null 2>&1; then
    echo "-> pactl bulundu, ses seviyesi kontrolü PulseAudio/PipeWire üzerinden çalışacak."
elif command -v amixer >/dev/null 2>&1; then
    echo "-> pactl yok ama amixer var, ses seviyesi kontrolü ALSA üzerinden çalışacak."
else
    echo "-> UYARI: ne pactl ne amixer bulunamadı, ses seviyesi kontrolü çalışmayacak."
    echo "   (alsa-utils zaten kuruldu, amixer onunla gelmeliydi - kontrol et: which amixer)"
fi

INSTALL_DIR="$HOME/wfas-client-native"
mkdir -p "$INSTALL_DIR"
SRC_DIR="$(dirname "$0")"
cp "$SRC_DIR/wfas_native_client.py" "$INSTALL_DIR/"
cp "$SRC_DIR/wfas_tui.py" "$INSTALL_DIR/"
cp "$SRC_DIR/wfas-launch.sh" "$INSTALL_DIR/"
chmod +x "$INSTALL_DIR/wfas_native_client.py" "$INSTALL_DIR/wfas_tui.py" "$INSTALL_DIR/wfas-launch.sh"

mkdir -p "$HOME/.config/systemd/user"
cp "$SRC_DIR/wfas-native-client.service" "$HOME/.config/systemd/user/"

SYSTEMD_OK=0
if command -v systemctl >/dev/null 2>&1 && systemctl --user daemon-reload >/dev/null 2>&1; then
    SYSTEMD_OK=1
    loginctl enable-linger "$USER" 2>/dev/null || true
    echo "-> systemd --user bu oturumda çalışıyor, servis kaydedildi."
else
    echo "-> UYARI: systemd --user bu oturumda çalışmıyor (muhtemelen SysVinit ile açıldınız)."
    echo "   Sorun değil: wfas-launch.sh bunu algılayıp istemciyi doğrudan arka planda başlatacak."
fi

FLUXBOX_DIR="$HOME/.fluxbox"
STARTUP="$FLUXBOX_DIR/startup"
mkdir -p "$FLUXBOX_DIR"

if [ ! -f "$STARTUP" ]; then
    printf '#!/bin/sh\n\nexec fluxbox\n' > "$STARTUP"
    chmod +x "$STARTUP"
fi

MARKER="# >>> wfas-native-client tui autostart >>>"
if ! grep -qF "$MARKER" "$STARTUP"; then
    cp "$STARTUP" "$STARTUP.bak.$(date +%s)"
    TMP="$(mktemp)"
    awk -v marker="$MARKER" -v launcher="$INSTALL_DIR/wfas-launch.sh" '
        /^[[:space:]]*exec[[:space:]]+fluxbox/ && !done {
            print marker
            print "if command -v xfce4-terminal >/dev/null 2>&1; then"
            print "    xfce4-terminal --title=\"WFAS Durumu\" --geometry=60x14 -e \"" launcher "\" &"
            print "elif command -v urxvt >/dev/null 2>&1; then"
            print "    urxvt -title \"WFAS Durumu\" -geometry 60x14 -e \"" launcher "\" &"
            print "elif command -v lxterminal >/dev/null 2>&1; then"
            print "    lxterminal --title=\"WFAS Durumu\" -e \"" launcher "\" &"
            print "else"
            print "    xterm -T \"WFAS Durumu\" -geometry 60x14 -e \"" launcher "\" &"
            print "fi"
            print "# <<< wfas-native-client tui autostart <<<"
            done = 1
        }
        { print }
    ' "$STARTUP" > "$TMP"
    mv "$TMP" "$STARTUP"
    chmod +x "$STARTUP"
    echo "-> Fluxbox startup güncellendi (yedek: $STARTUP.bak.*)"
else
    echo "-> Fluxbox startup zaten yapılandırılmış, dokunulmadı."
fi

echo ""
echo "Kurulum tamamlandı."
echo ""
echo "Önce elle test et:"
echo "  python3 $INSTALL_DIR/wfas_native_client.py"
echo ""
if [ "$SYSTEMD_OK" -eq 1 ]; then
    echo "Sorunsuz çalışıyorsa açılışta otomatik başlatmak için:"
    echo "  systemctl --user enable --now wfas-native-client.service"
    echo "  journalctl --user -u wfas-native-client -f"
    echo ""
fi
echo "Durum TUI'sini elle denemek için:"
echo "  python3 $INSTALL_DIR/wfas_tui.py"
echo ""
echo "Başlatıcıyı (servis + TUI birlikte) elle denemek için:"
echo "  $INSTALL_DIR/wfas-launch.sh"
echo ""
echo "Web arayüzü: http://localhost:8091"
echo ""
echo "ÖNEMLİ: Sunucu (masaüstü uygulaması) tarafında:"
echo "  1) Ayarlar -> 'Advertise HTTP stream' KAPALI olmalı"
echo "     (açıkken uygulama Multicast'i zorunlu kılar, Unicast anahtarı kilitli görünür)"
echo "  2) Multicast anahtarı KAPALI olmalı (yani Unicast)"
echo ""
echo "NOT: Fluxbox oturumunu yeniden başlatınca (X'i restart edince ya da"
echo "     yeniden login olunca) durum penceresi otomatik açılacak."
