#!/usr/bin/env bash
# WFAS client -> tek dosyalık AppImage (x86_64).
# Taşınabilir Python: https://github.com/niess/python-appimage (manylinux2014)
#
# Kullanım:
#   ./build-appimage.sh                     # tabanı GitHub'dan indirir (gh ya da curl)
#   PY_BASE=/yol/python3.11.x.AppImage ./build-appimage.sh   # hazır tabanı kullanır
# Çıktı: ./dist/WFAS-Client-x86_64.AppImage
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
CLIENT_DIR="$(cd "$HERE/.." && pwd)"
WORK="$HERE/build"
DIST="$HERE/dist"
PYSERIES="3.11"
rm -rf "$WORK"; mkdir -p "$WORK" "$DIST"
cd "$WORK"

# 1) Python tabanı
if [ -n "${PY_BASE:-}" ]; then
    cp "$PY_BASE" base.AppImage
else
    echo "== Python $PYSERIES tabanı çözülüyor =="
    NAME=""
    if command -v gh >/dev/null 2>&1 && [ -n "${GH_TOKEN:-}" ]; then
        NAME="$(gh api "repos/niess/python-appimage/releases/tags/python$PYSERIES" \
                 --jq '.assets[].name' | grep 'manylinux2014_x86_64.AppImage$' | sort -V | tail -1 || true)"
    fi
    if [ -z "$NAME" ]; then
        NAME="$(curl -fsSL "https://github.com/niess/python-appimage/releases/expanded_assets/python$PYSERIES" \
                 | grep -o "python$PYSERIES[^\"/]*manylinux2014_x86_64\.AppImage" | sort -uV | tail -1)"
    fi
    [ -n "$NAME" ] || { echo "Python tabanı bulunamadı" >&2; exit 1; }
    echo "-> $NAME"
    curl -fsSL -o base.AppImage \
        "https://github.com/niess/python-appimage/releases/download/python$PYSERIES/$NAME"
fi
chmod +x base.AppImage
./base.AppImage --appimage-extract >/dev/null
APPDIR="$WORK/squashfs-root"
[ -x "$APPDIR/opt/python$PYSERIES/bin/python$PYSERIES" ] || { echo "Beklenen Python bulunamadı" >&2; exit 1; }

# 2) Gereksizleri at (tk/idle/test) - istemci sadece stdlib + curses kullanıyor
STD="$APPDIR/opt/python$PYSERIES/lib/python$PYSERIES"
rm -rf "$STD/test" "$STD/idlelib" "$STD/tkinter" "$STD/turtledemo" "$STD/lib2to3" \
       "$STD"/lib-dynload/_tkinter*.so "$APPDIR/usr/share/tcltk" \
       "$APPDIR"/python3*.desktop "$APPDIR/python.png" \
       "$APPDIR"/usr/share/applications "$APPDIR"/usr/share/icons "$APPDIR"/usr/share/metainfo

# 3) Uygulama dosyaları
mkdir -p "$APPDIR/opt/wfas"
cp "$CLIENT_DIR/wfas_native_client.py" "$CLIENT_DIR/wfas_tui.py" "$APPDIR/opt/wfas/"
cp "$HERE/AppRun" "$APPDIR/AppRun"; chmod +x "$APPDIR/AppRun"

cat > "$APPDIR/wfas-client.desktop" <<'DESK'
[Desktop Entry]
Type=Application
Name=WFAS Client
Comment=WFAS UDP unicast ses alıcısı
Exec=AppRun --launch
Icon=wfas-client
Terminal=true
Categories=AudioVideo;Audio;
DESK
python3 "$HERE/make_icon.py" "$APPDIR/wfas-client.png"

# 4) appimagetool
if ! command -v appimagetool >/dev/null 2>&1; then
    curl -fsSL -o appimagetool \
        "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage"
    chmod +x appimagetool
    TOOL=(./appimagetool --appimage-extract-and-run)
else
    TOOL=(appimagetool)
fi
ARCH=x86_64 "${TOOL[@]}" --no-appstream "$APPDIR" "$DIST/WFAS-Client-x86_64.AppImage"
ls -lh "$DIST/WFAS-Client-x86_64.AppImage"
