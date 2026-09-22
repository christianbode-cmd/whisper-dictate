#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"

echo "==================================="
echo "  Blab — Build"
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
ICONSET_DIR="$SCRIPT_DIR/Blab.iconset"
mkdir -p "$ICONSET_DIR"

python3 << 'ICON_SCRIPT'
import AppKit
import os

sizes = [16, 32, 64, 128, 256, 512, 1024]
iconset_dir = os.environ.get("ICONSET_DIR", "Blab.iconset")

for size in sizes:
    img = AppKit.NSImage.alloc().initWithSize_((size, size))
    img.lockFocus()

    paper = AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(254/255, 243/255, 160/255, 1.0)
    ink = AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(18/255, 18/255, 18/255, 1.0)

    # Paper squircle with the standard macOS icon inset
    paper.setFill()
    inset = size * 0.08
    AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        ((inset, inset), (size - 2*inset, size - 2*inset)), size*0.2, size*0.2
    ).fill()

    # Ink speech bubble with a tail
    ink.setFill()
    AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        ((size*0.20, size*0.36), (size*0.60, size*0.40)), size*0.12, size*0.12
    ).fill()
    tail = AppKit.NSBezierPath.bezierPath()
    tail.moveToPoint_((size*0.30, size*0.40))
    tail.lineToPoint_((size*0.46, size*0.40))
    tail.lineToPoint_((size*0.27, size*0.21))
    tail.closePath()
    tail.fill()

    # Paper waveform inside the bubble
    paper.setFill()
    bar_w, gap = size*0.05, size*0.045
    heights = [0.08, 0.16, 0.24, 0.16, 0.08]
    x = size*0.5 - (len(heights)*bar_w + (len(heights)-1)*gap) / 2
    cy = size*0.56
    for h in heights:
        bh = size*h
        AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            ((x, cy - bh/2), (bar_w, bh)), bar_w/2, bar_w/2
        ).fill()
        x += bar_w + gap

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
    iconutil -c icns "$ICONSET_DIR" -o "$SCRIPT_DIR/Blab.icns" 2>/dev/null || true
    echo "Created Blab.icns"
fi
rm -rf "$ICONSET_DIR"

# ── Create py2app setup file ─────────────────────────────────────────────
cat > "$SCRIPT_DIR/setup_py2app.py" << 'SETUP_SCRIPT'
from setuptools import setup

APP = ['blab.py']
DATA_FILES = ['config.json']

OPTIONS = {
    'argv_emulation': False,
    'iconfile': 'Blab.icns',
    'plist': {
        'CFBundleName': 'Blab',
        'CFBundleDisplayName': 'Blab',
        'CFBundleIdentifier': 'io.github.christianbode-cmd.blab',
        'CFBundleVersion': '1.2.0',
        'CFBundleShortVersionString': '1.2.0',
        'LSMinimumSystemVersion': '13.0',
        'LSUIElement': True,
        'NSMicrophoneUsageDescription': 'Blab needs microphone access to record your voice for transcription.',
        'NSAppleEventsUsageDescription': 'Blab needs accessibility access to paste transcribed text into your applications.',
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

# config.json is gitignored; seed it from the example on a fresh checkout.
if [ ! -f "$SCRIPT_DIR/config.json" ]; then
    cp "$SCRIPT_DIR/config.example.json" "$SCRIPT_DIR/config.json"
    echo "Created config.json from config.example.json"
fi

# ── Build ─────────────────────────────────────────────────────────────────
echo ""
echo "Building Blab.app..."
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

APP_PATH="$SCRIPT_DIR/dist/Blab.app"
RESOURCES_DIR="$APP_PATH/Contents/Resources"

if [ -d "$APP_PATH" ]; then
    cp "$SCRIPT_DIR/config.json" "$RESOURCES_DIR/config.json"

    # Clear quarantine so macOS doesn't block it
    xattr -cr "$APP_PATH" 2>/dev/null || true

    # ── Build DMG ──────────────────────────────────────────────────────────
    DMG_NAME="Blab"
    DMG_PATH="$SCRIPT_DIR/dist/Blab.dmg"
    DMG_STAGING="$SCRIPT_DIR/dist/dmg_staging"
    DMG_TMP="$SCRIPT_DIR/dist/Blab_tmp.dmg"

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
    "Drag Blab to Applications to install"
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
        set position of item "Blab.app" to {160, 210}
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

    # Convert to compressed read-only DMG.  On recent macOS hdiutil convert
    # can fail with "Resource temporarily unavailable"; fall back to building
    # a plain (unstyled) DMG straight from the staging folder.
    if ! hdiutil convert "$DMG_TMP" -format UDZO -imagekey zlib-level=9 \
            -o "$DMG_PATH" > /dev/null 2>&1; then
        echo "hdiutil convert failed — building an unstyled DMG instead"
        rm -f "$DMG_PATH"
        hdiutil create -volname "$DMG_NAME" -srcfolder "$DMG_STAGING" -ov \
            -format UDZO -imagekey zlib-level=9 "$DMG_PATH" > /dev/null
    fi
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
    echo "  1. Open dist/Blab.dmg"
    echo "  2. Drag 'Blab' into the Applications folder"
    echo "  3. Launch from Applications or Spotlight"
    echo "  4. Grant Microphone + Accessibility when prompted"
    echo "  5. Enter your OpenAI API key in Preferences"
    echo ""
    echo "  NOTE: On first launch macOS may say the app cannot be opened."
    echo "  Right-click the app → Open → Open to bypass this once."
    echo ""
    echo "  DEBUGGING:"
    echo "  tail -f ~/Library/Logs/Blab.log"
    echo ""
else
    echo "Error: Build failed. Check output above."
    exit 1
fi

# Cleanup
rm -rf "$SCRIPT_DIR/build"
rm -f "$SCRIPT_DIR/setup_py2app.py"
rm -f "$SCRIPT_DIR/Blab.icns"
