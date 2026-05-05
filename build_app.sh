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
    DMG_TMP="$SCRIPT_DIR/dist/WD_tmp.dmg"

    echo "Creating DMG installer..."

    rm -rf "$DMG_STAGING"
    mkdir -p "$DMG_STAGING/.background"

    # Generate background image using AppKit (already available in the venv)
    "$VENV_DIR/bin/python3" << 'BG_SCRIPT'
import AppKit, Foundation

W, H = 600, 380
img = AppKit.NSImage.alloc().initWithSize_((W, H))
img.lockFocus()

# Background — light warm-grey
AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(0.93, 0.93, 0.95, 1.0).setFill()
AppKit.NSBezierPath.fillRect_(((0, 0), (W, H)))

# Arrow between icon positions (AppKit origin is bottom-left)
arrow = AppKit.NSBezierPath.bezierPath()
arrow.setLineWidth_(2.5)
arrow.moveToPoint_((245, 210))
arrow.lineToPoint_((355, 210))
arrow.moveToPoint_((340, 196))
arrow.lineToPoint_((355, 210))
arrow.lineToPoint_((340, 224))
AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(0.5, 0.5, 0.55, 1.0).setStroke()
arrow.stroke()

# Centered paragraph style
para = AppKit.NSMutableParagraphStyle.alloc().init()
para.setAlignment_(AppKit.NSTextAlignmentCenter)

# Main instruction
Foundation.NSString.stringWithString_(
    "Drag Whisper Dictate to Applications to install"
).drawInRect_withAttributes_(((0, 108), (W, 24)), {
    AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_(13),
    AppKit.NSForegroundColorAttributeName: AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(0.2, 0.2, 0.25, 1.0),
    AppKit.NSParagraphStyleAttributeName: para,
})

# Gatekeeper note
Foundation.NSString.stringWithString_(
    "First launch: right-click → Open if macOS says the app cannot be opened"
).drawInRect_withAttributes_(((0, 56), (W, 22)), {
    AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_(11),
    AppKit.NSForegroundColorAttributeName: AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(0.5, 0.5, 0.55, 1.0),
    AppKit.NSParagraphStyleAttributeName: para,
})

img.unlockFocus()

tiff = img.TIFFRepresentation()
bitmap = AppKit.NSBitmapImageRep.alloc().initWithData_(tiff)
png = bitmap.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, {})
png.writeToFile_atomically_("dist/dmg_staging/.background/background.png", True)
print("Background image created.")
BG_SCRIPT

    # Copy app and Applications symlink
    cp -R "$APP_PATH" "$DMG_STAGING/"
    ln -s /Applications "$DMG_STAGING/Applications"

    rm -f "$DMG_PATH" "$DMG_TMP"

    # Create a read-write HFS+ DMG so Finder background images work
    hdiutil create \
        -volname "$DMG_NAME" \
        -srcfolder "$DMG_STAGING" \
        -fs HFS+J \
        -ov -format UDRW \
        "$DMG_TMP" > /dev/null

    # Mount it (volume name = DMG_NAME → /Volumes/$DMG_NAME)
    MOUNT_POINT="/Volumes/$DMG_NAME"
    hdiutil attach -readwrite -noverify -noautoopen "$DMG_TMP" > /dev/null

    sleep 3

    # Configure the Finder window: background, icon positions, window size.
    # Retry loop handles the case where Finder hasn't registered the disk yet.
    osascript << APPLESCRIPT
tell application "Finder"
    set myDisk to missing value
    repeat 10 times
        try
            set myDisk to disk "$DMG_NAME"
            exit repeat
        on error
            delay 1
        end try
    end repeat
    if myDisk is missing value then error "Disk not found"
    tell myDisk
        open
        set current view of container window to icon view
        set toolbar visible of container window to false
        set statusbar visible of container window to false
        set bounds of container window to {200, 120, 800, 500}
        set theViewOptions to icon view options of container window
        set arrangement of theViewOptions to not arranged
        set icon size of theViewOptions to 100
        set background picture of theViewOptions to (alias POSIX file "$MOUNT_POINT/.background/background.png")
        set position of item "Whisper Dictate.app" to {160, 210}
        set position of item "Applications" to {440, 210}
        close
        open
        update without registering applications
        delay 2
        close
    end tell
end tell
APPLESCRIPT

    sleep 2
    # Eject via Finder first, then force-detach as fallback
    osascript -e "tell application \"Finder\" to if disk \"$DMG_NAME\" exists then eject disk \"$DMG_NAME\"" 2>/dev/null || true
    sleep 2
    hdiutil detach "$MOUNT_POINT" -force > /dev/null 2>&1 || true
    # Wait until the volume is fully gone before converting
    for i in $(seq 1 10); do
        mount | grep -q "$MOUNT_POINT" || break
        sleep 1
    done

    # Convert to compressed read-only DMG
    hdiutil convert "$DMG_TMP" -format UDZO -imagekey zlib-level=9 \
        -o "$DMG_PATH" > /dev/null
    rm -f "$DMG_TMP"
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
