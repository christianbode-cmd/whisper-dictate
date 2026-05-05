#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"

echo "==================================="
echo "  Whisper Dictate — Build"
echo "==================================="
echo ""

# ── Check Python ──────────────────────────────────────────────────────────
PYTHON=$(command -v python3 || true)
if [ -z "$PYTHON" ]; then
    echo "Error: python3 not found. Install Python 3.9+ first."
    exit 1
fi
PY_VERSION=$($PYTHON -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "Found Python $PY_VERSION"

# ── Create venv & install deps ────────────────────────────────────────────
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment..."
    $PYTHON -m venv "$VENV_DIR"
fi
source "$VENV_DIR/bin/activate"

echo "Installing dependencies..."
pip install --quiet --upgrade pip
pip install --quiet \
    pyobjc-framework-Cocoa \
    pyobjc-framework-AVFoundation \
    pyobjc-framework-Quartz \
    openai \
    py2app

# ── Create app icon ──────────────────────────────────────────────────────
echo "Creating app icon..."
ICONSET_DIR="$SCRIPT_DIR/WhisperDictate.iconset"
mkdir -p "$ICONSET_DIR"

python3 << 'ICON_SCRIPT'
import AppKit
import os

sizes = [16, 32, 64, 128, 256, 512, 1024]
iconset_dir = os.environ.get("ICONSET_DIR", "WhisperDictate.iconset")

for size in sizes:
    img = AppKit.NSImage.alloc().initWithSize_((size, size))
    img.lockFocus()

    bg = AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(0.18, 0.55, 0.82, 1.0)
    bg.setFill()
    circle = AppKit.NSBezierPath.bezierPathWithOvalInRect_(((size*0.05, size*0.05), (size*0.9, size*0.9)))
    circle.fill()

    white = AppKit.NSColor.whiteColor()
    white.setFill()
    mic_w = size * 0.22
    mic_h = size * 0.35
    mic_x = (size - mic_w) / 2
    mic_y = size * 0.42
    mic_rect = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        ((mic_x, mic_y), (mic_w, mic_h)), mic_w/2, mic_w/2
    )
    mic_rect.fill()

    white.setStroke()
    arc_path = AppKit.NSBezierPath.bezierPath()
    arc_path.setLineWidth_(size * 0.04)
    arc_w = size * 0.34
    arc_path.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_clockwise_(
        (size/2, mic_y + mic_h * 0.3), arc_w/2, 220, 320, True
    )
    arc_path.stroke()

    stand = AppKit.NSBezierPath.bezierPath()
    stand.setLineWidth_(size * 0.04)
    stand.moveToPoint_((size/2, mic_y - size * 0.02))
    stand.lineToPoint_((size/2, size * 0.22))
    stand.stroke()

    base = AppKit.NSBezierPath.bezierPath()
    base.setLineWidth_(size * 0.04)
    base.moveToPoint_((size * 0.35, size * 0.22))
    base.lineToPoint_((size * 0.65, size * 0.22))
    base.stroke()

    img.unlockFocus()

    tiff = img.TIFFRepresentation()
    bitmap = AppKit.NSBitmapImageRep.alloc().initWithData_(tiff)
    png_data = bitmap.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, {})

    if size <= 512:
        filename = f"icon_{size}x{size}.png"
        png_data.writeToFile_atomically_(os.path.join(iconset_dir, filename), True)
    if size >= 32:
        half = size // 2
        filename = f"icon_{half}x{half}@2x.png"
        png_data.writeToFile_atomically_(os.path.join(iconset_dir, filename), True)

print("Icon PNGs created.")
ICON_SCRIPT

if command -v iconutil &> /dev/null; then
    iconutil -c icns "$ICONSET_DIR" -o "$SCRIPT_DIR/WhisperDictate.icns" 2>/dev/null || true
    echo "Created WhisperDictate.icns"
fi
rm -rf "$ICONSET_DIR"

# ── Create py2app setup file ─────────────────────────────────────────────
cat > "$SCRIPT_DIR/setup_py2app.py" << 'SETUP_SCRIPT'
from setuptools import setup

APP = ['whisper_dictate.py']
DATA_FILES = ['config.json']

OPTIONS = {
    'argv_emulation': False,
    'iconfile': 'WhisperDictate.icns',
    'plist': {
        'CFBundleName': 'Whisper Dictate',
        'CFBundleDisplayName': 'Whisper Dictate',
        'CFBundleIdentifier': 'com.whisper-dictate.app',
        'CFBundleVersion': '1.0.0',
        'CFBundleShortVersionString': '1.0.0',
        'LSMinimumSystemVersion': '13.0',
        'LSUIElement': True,
        'NSMicrophoneUsageDescription': 'Whisper Dictate needs microphone access to record your voice for transcription.',
        'NSAppleEventsUsageDescription': 'Whisper Dictate needs accessibility access to paste transcribed text into your applications.',
    },
    'packages': ['openai', 'httpx', 'httpcore', 'certifi', 'idna', 'sniffio', 'anyio', 'h11', 'pydantic', 'pydantic_core', 'annotated_types', 'distro', 'jiter', 'typing_extensions'],
    'frameworks': [],
}

setup(
    app=APP,
    data_files=DATA_FILES,
    options={'py2app': OPTIONS},
    setup_requires=['py2app'],
)
SETUP_SCRIPT

# ── Build ─────────────────────────────────────────────────────────────────
echo ""
echo "Building Whisper Dictate.app..."
echo "(this may take a minute)"
echo ""

cd "$SCRIPT_DIR"

# Clean previous build.
# Use mv-then-delete so a running .app or Spotlight lock on the old dist
# doesn't abort the build.  The mv is atomic; rm -rf runs in the background.
rm -rf "$SCRIPT_DIR/build"
if [ -d "$SCRIPT_DIR/dist" ]; then
    mv "$SCRIPT_DIR/dist" "$SCRIPT_DIR/dist_old_$$"
    { rm -rf "$SCRIPT_DIR/dist_old_$$" 2>/dev/null || true; } &
fi

python setup_py2app.py py2app --dist-dir "$SCRIPT_DIR/dist" 2>&1 | tail -5

APP_PATH="$SCRIPT_DIR/dist/Whisper Dictate.app"
RESOURCES_DIR="$APP_PATH/Contents/Resources"

if [ -d "$APP_PATH" ]; then
    cp "$SCRIPT_DIR/config.json" "$RESOURCES_DIR/config.json"

    # Clear quarantine so macOS doesn't block it
    xattr -cr "$APP_PATH" 2>/dev/null || true

    # ── Build DMG ──────────────────────────────────────────────────────────
    DMG_NAME="Whisper Dictate"
    DMG_PATH="$SCRIPT_DIR/dist/Whisper Dictate.dmg"
    DMG_STAGING="$SCRIPT_DIR/dist/dmg_staging"

    echo "Creating DMG installer..."

    rm -rf "$DMG_STAGING"
    mkdir -p "$DMG_STAGING"

    # Copy app and add /Applications symlink for drag-to-install
    cp -R "$APP_PATH" "$DMG_STAGING/"
    ln -s /Applications "$DMG_STAGING/Applications"

    # Remove any previous DMG
    rm -f "$DMG_PATH"

    hdiutil create \
        -volname "$DMG_NAME" \
        -srcfolder "$DMG_STAGING" \
        -ov \
        -format UDZO \
        -imagekey zlib-level=9 \
        "$DMG_PATH" > /dev/null

    rm -rf "$DMG_STAGING"

    echo ""
    echo "==================================="
    echo "  Build complete!"
    echo "==================================="
    echo ""
    echo "  App: $APP_PATH"
    echo "  DMG: $DMG_PATH"
    echo ""
    echo "  INSTALL (no terminal needed):"
    echo "  1. Open dist/Whisper Dictate.dmg"
    echo "  2. Drag 'Whisper Dictate' into the Applications folder"
    echo "  3. Launch from Applications or Spotlight"
    echo "  4. Grant Microphone + Accessibility when prompted"
    echo "  5. Enter your OpenAI API key in Preferences"
    echo ""
    echo "  NOTE: On first launch macOS may say the app cannot be opened."
    echo "  Right-click the app → Open → Open to bypass this once."
    echo ""
    echo "  DEBUGGING:"
    echo "  tail -f ~/Library/Logs/WhisperDictate.log"
    echo ""
else
    echo "Error: Build failed. Check output above."
    exit 1
fi

# Cleanup
rm -rf "$SCRIPT_DIR/build"
rm -f "$SCRIPT_DIR/setup_py2app.py"
rm -f "$SCRIPT_DIR/WhisperDictate.icns"
