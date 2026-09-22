#!/usr/bin/env python3
"""
Blab — hold-to-record voice transcription for macOS.

Hold a hotkey, speak, release → transcribed text is pasted into the focused field.
Uses OpenAI's transcription API and native macOS APIs throughout.
"""

import json
import logging
import os
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time

# ---------------------------------------------------------------------------
# Logging — writes to ~/Library/Logs/Blab.log
# Visible even when running as a .app bundle with no terminal
# ---------------------------------------------------------------------------
LOG_DIR = os.path.expanduser("~/Library/Logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_PATH = os.path.join(LOG_DIR, "Blab.log")

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("Blab")
log.info(f"Log file: {LOG_PATH}")

# ---------------------------------------------------------------------------
# Dependency check
# ---------------------------------------------------------------------------
REQUIRED = ["objc", "AppKit", "Foundation", "AVFoundation", "Quartz", "openai"]

def check_dependencies():
    missing = []
    for mod in REQUIRED:
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    if missing:
        log.error(f"Missing dependencies: {', '.join(missing)}")
        log.error("Run: pip install pyobjc-framework-Cocoa pyobjc-framework-AVFoundation pyobjc-framework-Quartz openai")
        sys.exit(1)

check_dependencies()

import objc
import AppKit
import Foundation
import AVFoundation
import Quartz
from openai import OpenAI

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
def find_config_path():
    """Find config.json — check alongside script, then .app bundle, then home."""
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    if os.path.exists(p):
        return p

    p2 = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "Resources", "config.json")
    if os.path.exists(p2):
        return os.path.abspath(p2)

    p3 = os.path.expanduser("~/blab/config.json")
    if os.path.exists(p3):
        return p3

    return p

CONFIG_PATH = find_config_path()
log.info(f"Config path: {CONFIG_PATH}")

DEFAULT_CONFIG = {
    "hotkey_keycode": 58,
    "model": "gpt-transcribe",
    "input_device": "default",
    "language": "en",
    "response_format": "text",
    "prompt": "",
    "sound_on_start": True,
    "sound_on_stop": True,
}

# ---------------------------------------------------------------------------
# Keychain — API key storage
# ---------------------------------------------------------------------------
_KC_SERVICE = "Blab"
_KC_LEGACY_SERVICE = "WhisperDictate"   # pre-1.2.0 item, migrated on first read
_KC_ACCOUNT = "OpenAIAPIKey"

# Cached OpenAI client — invalidated by keychain_save_api_key().
_openai_client: "OpenAI | None" = None

def _keychain_read(service):
    try:
        result = subprocess.run(
            ["security", "find-generic-password",
             "-s", service, "-a", _KC_ACCOUNT, "-w"],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            return result.stdout.strip() or None
    except Exception as e:
        log.warning(f"Keychain read error: {e}")
    return None

def keychain_get_api_key():
    """Return the stored API key, or None if not set."""
    key = _keychain_read(_KC_SERVICE)
    if key is None:
        key = _keychain_read(_KC_LEGACY_SERVICE)
        if key and keychain_save_api_key(key):
            subprocess.run(
                ["security", "delete-generic-password",
                 "-s", _KC_LEGACY_SERVICE, "-a", _KC_ACCOUNT],
                capture_output=True,
            )
            log.info(f"Migrated API key from the {_KC_LEGACY_SERVICE} Keychain item")
    return key

def _get_openai_client():
    """Return a cached OpenAI client, or None if no API key is stored."""
    global _openai_client
    if _openai_client is None:
        api_key = keychain_get_api_key()
        if not api_key:
            return None
        # Short timeout: a hung request would otherwise lock the hotkey for
        # the SDK default of 10 minutes.
        _openai_client = OpenAI(api_key=api_key, timeout=30.0, max_retries=1)
        log.info("OpenAI client initialised")
    return _openai_client

def keychain_save_api_key(key):
    """Store (or delete) the API key in the macOS Keychain."""
    global _openai_client
    _openai_client = None   # invalidate cached client
    try:
        subprocess.run(
            ["security", "delete-generic-password",
             "-s", _KC_SERVICE, "-a", _KC_ACCOUNT],
            capture_output=True,
        )
        if key:
            result = subprocess.run(
                ["security", "add-generic-password",
                 "-s", _KC_SERVICE, "-a", _KC_ACCOUNT, "-w", key],
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                log.error(f"Keychain write failed: {result.stderr.strip()}")
                return False
        log.info("API key saved to Keychain")
        return True
    except Exception as e:
        log.error(f"Keychain write error: {e}")
        return False

def _truncate_api_key(key):
    """Return a display-safe version of the key: first 7 chars + ... + last 4."""
    if not key or len(key) < 12:
        return key or ""
    return f"{key[:7]}...{key[-4:]}"

def load_config():
    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        log.info(f"Config created at {CONFIG_PATH}")

    with open(CONFIG_PATH) as f:
        cfg = json.load(f)

    merged = {**DEFAULT_CONFIG, **cfg}

    # One-time migration: if the key is still in config.json, move it to
    # the Keychain and scrub it from the file.
    legacy_key = merged.pop("openai_api_key", None)
    if legacy_key and legacy_key not in ("", "sk-YOUR-KEY-HERE"):
        if not keychain_get_api_key():
            keychain_save_api_key(legacy_key)
            log.info("Migrated API key from config.json to Keychain")
        save_config(merged)

    log.info(f"Config loaded. Model: {merged['model']}, Mic: {merged['input_device']}, Language: {merged['language']}, Keycode: {merged['hotkey_keycode']}")
    return merged


def save_config(config):
    """Persist config dict to CONFIG_PATH. API key is never written here."""
    try:
        safe = {k: v for k, v in config.items() if k != "openai_api_key"}
        with open(CONFIG_PATH, "w") as f:
            json.dump(safe, f, indent=2)
        log.info(f"Config saved: {CONFIG_PATH}")
        return True
    except Exception as e:
        log.error(f"Failed to save config: {e}", exc_info=True)
        return False


# ---------------------------------------------------------------------------
# Keycode → human-readable name (used in Preferences UI)
# ---------------------------------------------------------------------------
KEYCODE_NAMES = {
    # Modifier keys
    58: "Left Option (⌥)",
    61: "Right Option (⌥)",
    59: "Left Control (⌃)",
    62: "Right Control (⌃)",
    56: "Left Shift (⇧)",
    60: "Right Shift (⇧)",
    55: "Left Command (⌘)",
    54: "Right Command (⌘)",
    63: "Fn",
    # Function keys
    122: "F1",
    120: "F2",
    99:  "F3",
    118: "F4",
    96:  "F5",
    97:  "F6",
    98:  "F7",
    100: "F8",
    101: "F9",
    109: "F10",
    103: "F11",
    111: "F12",
}

def keycode_to_name(keycode):
    return KEYCODE_NAMES.get(keycode, f"Key {keycode}")

# ---------------------------------------------------------------------------
# Audio input devices
# ---------------------------------------------------------------------------
DEFAULT_INPUT_DEVICE = "default"   # follow the system default input

def list_input_devices():
    """Return [(unique_id, name), ...] for every connected microphone."""
    if hasattr(AVFoundation, "AVCaptureDeviceTypeMicrophone"):
        types = [AVFoundation.AVCaptureDeviceTypeMicrophone]
    else:  # macOS < 14
        types = [AVFoundation.AVCaptureDeviceTypeBuiltInMicrophone,
                 AVFoundation.AVCaptureDeviceTypeExternalUnknown]
    discovery = AVFoundation.AVCaptureDeviceDiscoverySession.discoverySessionWithDeviceTypes_mediaType_position_(
        types, AVFoundation.AVMediaTypeAudio, AVFoundation.AVCaptureDevicePositionUnspecified
    )
    return [(str(d.uniqueID()), str(d.localizedName())) for d in discovery.devices()]


def resolve_input_device(unique_id):
    """Return the AVCaptureDevice for unique_id, falling back to the system default."""
    if unique_id and unique_id != DEFAULT_INPUT_DEVICE:
        device = AVFoundation.AVCaptureDevice.deviceWithUniqueID_(unique_id)
        if device is not None and device.isConnected():
            return device
        log.warning(f"Configured microphone {unique_id!r} not connected — using system default")
    return AVFoundation.AVCaptureDevice.defaultDeviceWithMediaType_(AVFoundation.AVMediaTypeAudio)


# ---------------------------------------------------------------------------
# Audio recorder using AVCaptureSession
#
# AVAudioRecorder always records from the system default input and cannot be
# pointed at a specific device.  With Bluetooth headsets (AirPods) that means
# every recording waits ~0.5-1 s for the A2DP->HFP profile switch, losing the
# start of the speech.  AVCaptureSession lets the user pin e.g. the built-in
# microphone instead.
# ---------------------------------------------------------------------------
class _RecordingDelegate(AppKit.NSObject, protocols=[objc.protocolNamed("AVCaptureFileOutputRecordingDelegate")]):
    """Signals when AVCaptureAudioFileOutput has finished writing the file."""

    def init(self):
        self = objc.super(_RecordingDelegate, self).init()
        if self is None:
            return None
        self.finished = threading.Event()
        self.error = None
        return self

    def captureOutput_didFinishRecordingToOutputFileAtURL_fromConnections_error_(
        self, output, url, connections, error
    ):
        self.error = error
        self.finished.set()


class AudioRecorder:
    # 16 kHz mono 16-bit PCM — small files, all the bandwidth speech needs.
    _SETTINGS = {
        AVFoundation.AVFormatIDKey: int(AVFoundation.kAudioFormatLinearPCM),
        AVFoundation.AVSampleRateKey: 16000.0,
        AVFoundation.AVNumberOfChannelsKey: 1,
        AVFoundation.AVLinearPCMBitDepthKey: 16,
        AVFoundation.AVLinearPCMIsFloatKey: False,
        AVFoundation.AVLinearPCMIsBigEndianKey: False,
    }

    def __init__(self, input_device=DEFAULT_INPUT_DEVICE):
        self.input_device = input_device
        self.filepath = os.path.join(tempfile.gettempdir(), "blab_recording.wav")
        self._session = None
        self._output = None
        self._device_uid = None
        self._delegate = None
        self._start_time = None
        self._prepared = False
        self._recording = False

    # All methods except wait_until_finalized() must run on the main thread.
    # AVCaptureSession.stopRunning() blocks on the main queue internally, so
    # calling it from a background thread while the main thread is busy
    # deadlocks.  Session setup is cheap (~15 ms), so main is fine.

    def prepare(self):
        """Tear down the previous session and build a fresh one for the
        configured device, so start() only has to start it.
        """
        if self._recording:
            log.warning("prepare() called while recording — ignored")
            return False
        self._prepared = False
        self._teardown()
        self.cleanup()

        device = resolve_input_device(self.input_device)
        if device is None:
            log.error("prepare(): no audio input device available")
            return False

        session = AVFoundation.AVCaptureSession.alloc().init()
        session.beginConfiguration()
        device_input, error = AVFoundation.AVCaptureDeviceInput.deviceInputWithDevice_error_(device, None)
        if error or device_input is None or not session.canAddInput_(device_input):
            log.error(f"prepare(): cannot use input '{device.localizedName()}': {error}")
            return False
        session.addInput_(device_input)

        output = AVFoundation.AVCaptureAudioFileOutput.alloc().init()
        if not session.canAddOutput_(output):
            log.error("prepare(): cannot add audio file output")
            return False
        session.addOutput_(output)
        session.commitConfiguration()
        # audioSettings is silently ignored unless set after the output has
        # joined the session.
        output.setAudioSettings_(self._SETTINGS)

        self._session = session
        self._output = output
        self._device_uid = str(device.uniqueID())
        self._prepared = True
        log.info(f"AudioRecorder ready on '{device.localizedName()}'")
        return True

    def _teardown(self):
        if self._session is not None and self._session.isRunning():
            self._session.stopRunning()
        self._session = None
        self._output = None

    def _default_device_changed(self):
        current = AVFoundation.AVCaptureDevice.defaultDeviceWithMediaType_(AVFoundation.AVMediaTypeAudio)
        return current is not None and str(current.uniqueID()) != self._device_uid

    def start(self):
        if not self._prepared:
            log.warning("start() called without prior prepare() — initialising inline")
            if not self.prepare():
                return False
        # In system-default mode the default may have changed since
        # prepare() ran (e.g. AirPods connected) — follow it.
        elif self.input_device == DEFAULT_INPUT_DEVICE and self._default_device_changed():
            log.info("Default input device changed — rebuilding session")
            if not self.prepare():
                return False

        self._session.startRunning()
        if not self._session.isRunning():
            log.error("AVCaptureSession failed to start")
            return False

        self._delegate = _RecordingDelegate.alloc().init()
        self._output.startRecordingToOutputFileURL_outputFileType_recordingDelegate_(
            Foundation.NSURL.fileURLWithPath_(self.filepath),
            AVFoundation.AVFileTypeWAVE,
            self._delegate,
        )
        self._start_time = time.time()
        self._recording = True
        self._prepared = False  # session is in use; prepare() needed next time
        log.info("Recording started")
        return True

    def get_level(self):
        """Return normalized audio level 0.0 (silence) to 1.0 (maximum)."""
        output = self._output
        if output is None or not output.isRecording():
            return 0.0
        connections = output.connections()
        channels = connections[0].audioChannels() if connections else None
        if not channels:
            return 0.0
        db = channels[0].averagePowerLevel()
        # Reported in dB from ~-160 (silence) to 0 (max).
        # Map the -60 dB to 0 dB range → 0.0 to 1.0 (anything quieter reads as 0).
        MIN_DB = -60.0
        clamped = max(MIN_DB, min(0.0, float(db)))
        return (clamped - MIN_DB) / (-MIN_DB)

    def stop(self):
        """Stop recording and return (filepath, duration).

        The file is finalised asynchronously — call wait_until_finalized()
        from a background thread before reading it.
        """
        duration = 0
        if self._recording:
            # isRecording() stays False until the first sample lands (~20 ms),
            # so rely on our own flag or a very short tap would never stop.
            self._output.stopRecording()
            self._recording = False
            duration = time.time() - self._start_time
        log.info(f"Recording stopped. Duration: {duration:.1f}s, File: {self.filepath}")
        return self.filepath, duration

    def wait_until_finalized(self, timeout=3.0):
        """Block until the recording file is fully written.

        The finish callback is delivered on the main thread, so this must
        never be called from there.
        """
        delegate = self._delegate
        if delegate is None:
            return True
        if not delegate.finished.wait(timeout):
            log.warning("Timed out waiting for the recording file to finalise")
            return False
        if delegate.error:
            log.error(f"Recording finished with error: {delegate.error}")
            return False
        return True

    def cleanup(self):
        if os.path.exists(self.filepath):
            try:
                os.remove(self.filepath)
            except OSError:
                pass

# ---------------------------------------------------------------------------
# Clipboard helper — save, write, paste, restore
# ---------------------------------------------------------------------------
class ClipboardPaster:
    @staticmethod
    def paste(text):
        log.info(f"Pasting text ({len(text)} chars)")
        pb = AppKit.NSPasteboard.generalPasteboard()

        old_string = pb.stringForType_(AppKit.NSPasteboardTypeString)

        pb.clearContents()
        pb.setString_forType_(text, AppKit.NSPasteboardTypeString)

        time.sleep(0.1)

        event = Quartz.CGEventCreateKeyboardEvent(None, 0x09, True)
        Quartz.CGEventSetFlags(event, Quartz.kCGEventFlagMaskCommand)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)

        event = Quartz.CGEventCreateKeyboardEvent(None, 0x09, False)
        Quartz.CGEventSetFlags(event, Quartz.kCGEventFlagMaskCommand)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
        log.info("Simulated Cmd+V")

        # Restore clipboard in a background thread so the caller is unblocked
        # immediately after Cmd+V fires.  The 1s delay gives slow apps (Slack,
        # Electron editors) time to read the pasteboard before we overwrite it.
        def _restore():
            time.sleep(1.0)
            pb.clearContents()
            if old_string:
                pb.setString_forType_(old_string, AppKit.NSPasteboardTypeString)
            log.debug("Clipboard restored")

        threading.Thread(target=_restore, daemon=True).start()

# ---------------------------------------------------------------------------
# Transcription
# ---------------------------------------------------------------------------
def transcribe(filepath, config):
    client = _get_openai_client()
    if not client:
        log.error("No API key in Keychain — open Preferences to add one")
        return None

    if not os.path.exists(filepath):
        log.error(f"Recording file not found: {filepath}")
        return None

    file_size = os.path.getsize(filepath)
    log.info(f"Sending to API. Model: {config['model']}, File size: {file_size} bytes")

    if file_size < 1000:
        log.warning(f"Recording file very small ({file_size} bytes) — may be empty/silent")

    kwargs = {
        "model": config["model"],
        "response_format": config["response_format"],
    }
    if config.get("language"):
        kwargs["language"] = config["language"]
    # prompt is only supported by whisper-1, not gpt-4o-transcribe
    if config.get("prompt") and config["model"] == "whisper-1":
        kwargs["prompt"] = config["prompt"]

    try:
        with open(filepath, "rb") as audio_file:
            kwargs["file"] = audio_file
            result = client.audio.transcriptions.create(**kwargs)

        if isinstance(result, str):
            text = result.strip()
        else:
            text = result.text.strip()

        log.info(f"Transcription result: '{text[:100]}{'...' if len(text) > 100 else ''}'")
        return text if text else None

    except Exception as e:
        log.error(f"API error: {type(e).__name__}: {e}", exc_info=True)
        return None

# ---------------------------------------------------------------------------
# System sounds
# ---------------------------------------------------------------------------
def play_sound(name):
    path = f"/System/Library/Sounds/{name}.aiff"
    if os.path.exists(path):
        sound = AppKit.NSSound.alloc().initWithContentsOfFile_byReference_(path, True)
        if sound:
            sound.play()

# ---------------------------------------------------------------------------
# Timer helper — ObjC class that NSTimer can call back into
# ---------------------------------------------------------------------------
class TimerHelper(AppKit.NSObject):
    _drain_fn = None

    def fire_(self, timer):
        if self._drain_fn:
            self._drain_fn()

# ---------------------------------------------------------------------------
# Audio level bar characters — used to render the VU meter in the menubar
# ---------------------------------------------------------------------------
_BAR_CHARS = "▁▂▃▄▅▆▇█"

# ---------------------------------------------------------------------------
# Menu action helper — thin ObjC target for the Preferences menu item
# ---------------------------------------------------------------------------
class AppDelegate(AppKit.NSObject):
    """Application delegate.

    Blab runs as a menubar (accessory) app, but the Preferences
    window temporarily switches the app to a Regular activation policy so its
    text fields can receive keyboard input.  Without this delegate, closing
    that window counts as "last window closed" and AppKit terminates the
    process — so the app would quit every time Preferences was closed.
    """

    def applicationShouldTerminateAfterLastWindowClosed_(self, sender):
        return False


class MenuActionHelper(AppKit.NSObject):
    """Bridges NSMenuItem actions to plain Python callbacks."""

    _callback = None  # class-level default so the attribute always exists

    def showPreferences_(self, sender):
        log.info("showPreferences_ called")
        if self._callback:
            self._callback()

    def validateMenuItem_(self, menu_item):
        """Always enable the Preferences menu item."""
        return True


# ---------------------------------------------------------------------------
# Visual style — pale paper, near-black ink, tracked monospace eyebrows
# ---------------------------------------------------------------------------
def _rgb(r, g, b, a=1.0):
    return AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(r / 255, g / 255, b / 255, a)

PAPER = _rgb(254, 243, 160)
INK = _rgb(18, 18, 18)
INK_SOFT = _rgb(18, 18, 18, 0.55)
CREAM_SOFT = _rgb(250, 247, 236, 0.65)

def _mono(size):
    return AppKit.NSFont.monospacedSystemFontOfSize_weight_(size, AppKit.NSFontWeightMedium)

def _tracked(text, font, color, kern=1.5):
    attrs = {
        AppKit.NSFontAttributeName: font,
        AppKit.NSForegroundColorAttributeName: color,
        AppKit.NSKernAttributeName: kern,
    }
    return Foundation.NSAttributedString.alloc().initWithString_attributes_(text, attrs)

def app_version():
    bundle = Foundation.NSBundle.mainBundle()
    if bundle.bundleIdentifier() != "io.github.christianbode-cmd.blab":
        return "dev"   # running from source: mainBundle is the Python framework
    return str(bundle.objectForInfoDictionaryKey_("CFBundleShortVersionString"))


# ---------------------------------------------------------------------------
# Preferences window — hotkey capture UI
# ---------------------------------------------------------------------------
class PreferencesWindowController(AppKit.NSObject):
    """
    Native macOS preferences panel.

    Call show_with_config(config, on_save) to display it.
    When the user clicks Save the new config is written to disk and
    on_save(new_config) is called so the app can re-register the hotkey.
    """

    _window = None  # set by show_with_config; cleared by windowWillClose_

    # ------------------------------------------------------------------
    # ObjC action methods (called by NSButton)
    # ------------------------------------------------------------------

    def startCapture_(self, sender):
        """Enter key-capture mode: next key/modifier press becomes the hotkey."""
        if self._capturing:
            return
        self._capturing = True
        self._set_pill_title(self._hotkey_btn, "Press a key…", INK_SOFT)
        self._hotkey_btn.setEnabled_(False)
        self._hint_label.setStringValue_("Press the key or modifier to use. Esc cancels.")

        mask = AppKit.NSEventMaskKeyDown | AppKit.NSEventMaskFlagsChanged
        ctrl = self  # closure reference

        def capture_handler(event):
            if not ctrl._capturing:
                return event
            etype = event.type()
            keycode = event.keyCode()

            if etype == AppKit.NSEventTypeFlagsChanged:
                # Fire only on key-press (flag set), not on key-release (flag cleared)
                flags = event.modifierFlags()
                modifier_map = {
                    58: AppKit.NSEventModifierFlagOption,
                    61: AppKit.NSEventModifierFlagOption,
                    59: AppKit.NSEventModifierFlagControl,
                    62: AppKit.NSEventModifierFlagControl,
                    56: AppKit.NSEventModifierFlagShift,
                    60: AppKit.NSEventModifierFlagShift,
                    55: AppKit.NSEventModifierFlagCommand,
                    54: AppKit.NSEventModifierFlagCommand,
                    63: AppKit.NSEventModifierFlagFunction,
                }
                flag = modifier_map.get(keycode)
                if flag and bool(flags & flag):
                    ctrl._finish_capture(keycode)
                return None  # consume event

            elif etype == AppKit.NSEventTypeKeyDown and not event.isARepeat():
                if keycode == 53:  # Escape — cancel capture, don't change hotkey
                    ctrl._cancel_capture()
                else:
                    ctrl._finish_capture(keycode)
                return None  # consume event

            return event

        self._capture_monitor = AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
            mask, capture_handler
        )

    def savePrefs_(self, sender):
        """Write config to disk, notify app, close window."""
        self._cleanup_capture()
        self._config["hotkey_keycode"] = self._pending_keycode
        self._config["model"] = self._model_popup.titleOfSelectedItem()
        self._config["input_device"] = self._mic_uids[self._mic_popup.indexOfSelectedItem()]
        lang = self._language_field.stringValue().strip()
        self._config["language"] = lang if lang else "en"
        api_key = self._api_key_field.stringValue().strip()
        if api_key != self._display_key:
            keychain_save_api_key(api_key)
        save_config(self._config)
        if self._on_save:
            self._on_save(self._config)
        self._window.close()

    def cancelPrefs_(self, sender):
        """Discard changes and close window."""
        self._cleanup_capture()
        self._window.close()

    def windowWillClose_(self, notification):
        """Restore accessory policy and clean up capture monitor on close."""
        self._cleanup_capture()
        self._window = None  # clear before AppKit auto-releases the window
        AppKit.NSApplication.sharedApplication().setActivationPolicy_(
            AppKit.NSApplicationActivationPolicyAccessory
        )

    def windowDidBecomeKey_(self, notification):
        if not self._capturing and hasattr(self, "_api_key_field"):
            self._window.makeFirstResponder_(self._api_key_field)

    # ------------------------------------------------------------------
    # Python helpers
    # ------------------------------------------------------------------

    def show_with_config(self, config, on_save):
        """Build and display the preferences window."""
        self._config = config
        self._on_save = on_save
        self._pending_keycode = config.get("hotkey_keycode", 58)
        self._capturing = False
        self._capture_monitor = None
        # Truncated placeholder shown in the field — used to detect whether
        # the user actually replaced the key or left it unchanged.
        self._display_key = _truncate_api_key(keychain_get_api_key())

        WIN_W, WIN_H = 480, 488
        M = 28                                  # outer margin
        COL = (WIN_W - 2 * M - 16) // 2         # two-column width
        style = (AppKit.NSWindowStyleMaskTitled
                 | AppKit.NSWindowStyleMaskClosable
                 | AppKit.NSWindowStyleMaskMiniaturizable
                 | AppKit.NSWindowStyleMaskFullSizeContentView)

        def R(x, top, w, h):
            """Rect from top-left coordinates (AppKit's origin is bottom-left)."""
            return Foundation.NSMakeRect(x, WIN_H - top - h, w, h)

        self._window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            Foundation.NSMakeRect(0, 0, WIN_W, WIN_H),
            style,
            AppKit.NSBackingStoreBuffered,
            False,
        )
        self._window.setTitle_("Blab — Preferences")
        self._window.setTitlebarAppearsTransparent_(True)
        self._window.setTitleVisibility_(AppKit.NSWindowTitleHidden)
        self._window.setBackgroundColor_(PAPER)
        self._window.setMovableByWindowBackground_(True)
        # Fixed light palette regardless of system dark mode
        self._window.setAppearance_(AppKit.NSAppearance.appearanceNamed_(AppKit.NSAppearanceNameAqua))
        # Do NOT let AppKit release the window when it closes.  PyObjC already
        # owns a reference (self._window); if AppKit also releases it the
        # window is over-released, which segfaults in the close animation
        # (-[_NSWindowTransformAnimation dealloc] → objc_release).  We clear
        # self._window in windowWillClose_, so PyObjC handles the lifetime.
        self._window.setReleasedWhenClosed_(False)
        self._window.setDelegate_(self)
        self._window.center()

        content = self._window.contentView()

        # Header
        content.addSubview_(self._eyebrow(R(M, 30, 240, 14), "Preferences"))
        content.addSubview_(self._label(
            R(M, 48, 360, 40), "Blab.",
            AppKit.NSFont.systemFontOfSize_weight_(30, AppKit.NSFontWeightHeavy), INK,
        ))
        version = self._eyebrow(R(WIN_W - M - 140, 56, 140, 14), f"v{app_version()}", upper=False)
        version.setAlignment_(AppKit.NSTextAlignmentRight)
        content.addSubview_(version)
        content.addSubview_(self._label(
            R(M, 92, WIN_W - 2 * M, 18),
            "Hold a key, speak, release — your words land where the cursor is.",
            AppKit.NSFont.systemFontOfSize_(12.5), INK_SOFT,
        ))

        # API key
        content.addSubview_(self._eyebrow(R(M, 134, COL, 14), "API key"))
        self._api_key_field = self._field(R(M, 152, WIN_W - 2 * M, 28), self._display_key, "sk-...")
        content.addSubview_(self._api_key_field)

        # Model (left column)
        content.addSubview_(self._eyebrow(R(M, 204, COL, 14), "Model"))
        self._model_popup = self._popup(R(M, 222, COL, 28))
        for m in ["gpt-transcribe", "gpt-4o-mini-transcribe", "gpt-4o-transcribe", "whisper-1"]:
            self._model_popup.addItemWithTitle_(m)
        current_model = config.get("model", "gpt-transcribe")
        # NOTE: selectItemWithTitle_ returns void (None), not a success flag,
        # so we must look up the index ourselves.  indexOfItemWithTitle_
        # returns -1 when the model isn't in the list.
        model_index = self._model_popup.indexOfItemWithTitle_(current_model)
        if model_index < 0:
            model_index = 0
        self._model_popup.selectItemAtIndex_(model_index)
        content.addSubview_(self._model_popup)

        # Microphone (right column).  Items are added via the menu directly
        # because NSPopUpButton.addItemWithTitle_ de-duplicates titles, which
        # would misalign two identically-named devices.
        content.addSubview_(self._eyebrow(R(M + COL + 16, 204, COL, 14), "Microphone"))
        self._mic_popup = self._popup(R(M + COL + 16, 222, COL, 28))
        self._mic_uids = [DEFAULT_INPUT_DEVICE]
        titles = ["System default"]
        for uid, name in list_input_devices():
            self._mic_uids.append(uid)
            titles.append(name)
        current_mic = config.get("input_device", DEFAULT_INPUT_DEVICE)
        if current_mic not in self._mic_uids:
            # Keep a disconnected device selectable so Save doesn't drop it.
            self._mic_uids.append(current_mic)
            titles.append(f"{current_mic} (not connected)")
        for title in titles:
            self._mic_popup.menu().addItemWithTitle_action_keyEquivalent_(title, None, "")
        self._mic_popup.selectItemAtIndex_(self._mic_uids.index(current_mic))
        content.addSubview_(self._mic_popup)

        # Language (left column)
        content.addSubview_(self._eyebrow(R(M, 274, COL, 14), "Language"))
        self._language_field = self._field(R(M, 290, 120, 28), config.get("language", "en"), "en")
        content.addSubview_(self._language_field)
        content.addSubview_(self._label(
            R(M, 328, COL, 16), "ISO code · en, de, fr, es", AppKit.NSFont.systemFontOfSize_(11), INK_SOFT,
        ))

        # Hotkey (right column): outlined pill that captures the next key press
        content.addSubview_(self._eyebrow(R(M + COL + 16, 274, COL, 14), "Hotkey"))
        self._hotkey_btn = self._pill(
            R(M + COL + 16, 290, COL, 32), keycode_to_name(self._pending_keycode),
            b"startCapture:", border=INK,
        )
        content.addSubview_(self._hotkey_btn)
        self._hint_label = self._label(
            R(M + COL + 16, 328, COL, 32), "Click, then press a key or modifier.",
            AppKit.NSFont.systemFontOfSize_(11), INK_SOFT, wrap=True,
        )
        content.addSubview_(self._hint_label)

        # Footer: ink bar with Cancel and the paper-coloured Save pill
        footer = AppKit.NSView.alloc().initWithFrame_(R(M, 388, WIN_W - 2 * M, 72))
        footer.setWantsLayer_(True)
        footer.layer().setBackgroundColor_(INK.CGColor())
        footer.layer().setCornerRadius_(20)
        content.addSubview_(footer)

        footer.addSubview_(self._pill(
            Foundation.NSMakeRect(22, 20, 120, 32), "ESC · CANCEL", b"cancelPrefs:",
            text_color=CREAM_SOFT, mono=True, key="\x1b",
        ))
        footer_w = footer.frame().size.width
        footer.addSubview_(self._pill(
            Foundation.NSMakeRect(footer_w - 22 - 124, 18, 124, 36), "Save  →", b"savePrefs:",
            fill=PAPER, text_color=INK, key="\r",
        ))

        # Switch to regular policy so this window can become a proper key
        # window and accept keyboard input (paste, typing).  Accessory-policy
        # apps cannot make their windows key, so text fields don't receive
        # keyboard events.  We restore the accessory policy on close.
        AppKit.NSApplication.sharedApplication().setActivationPolicy_(
            AppKit.NSApplicationActivationPolicyRegular
        )
        AppKit.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        self._window.makeKeyAndOrderFront_(None)
        self._window.makeFirstResponder_(self._api_key_field)
        log.info("Preferences window opened")

    def _finish_capture(self, keycode):
        self._pending_keycode = keycode
        self._capturing = False
        if self._capture_monitor:
            AppKit.NSEvent.removeMonitor_(self._capture_monitor)
            self._capture_monitor = None
        self._set_pill_title(self._hotkey_btn, keycode_to_name(keycode), INK)
        self._hotkey_btn.setEnabled_(True)
        self._hint_label.setStringValue_("Click, then press a key or modifier.")
        log.debug(f"Hotkey captured: {keycode} ({keycode_to_name(keycode)})")

    def _cancel_capture(self):
        self._capturing = False
        if self._capture_monitor:
            AppKit.NSEvent.removeMonitor_(self._capture_monitor)
            self._capture_monitor = None
        self._set_pill_title(self._hotkey_btn, keycode_to_name(self._pending_keycode), INK)
        self._hotkey_btn.setEnabled_(True)
        self._hint_label.setStringValue_("Click, then press a key or modifier.")

    def _cleanup_capture(self):
        if self._capture_monitor:
            AppKit.NSEvent.removeMonitor_(self._capture_monitor)
            self._capture_monitor = None
        self._capturing = False

    # ------------------------------------------------------------------
    # View factories
    # ------------------------------------------------------------------

    @objc.python_method
    def _label(self, frame, text, font, color, wrap=False):
        tf = (AppKit.NSTextField.wrappingLabelWithString_(text) if wrap
              else AppKit.NSTextField.labelWithString_(text))
        tf.setFrame_(frame)
        tf.setFont_(font)
        tf.setTextColor_(color)
        tf.setSelectable_(False)
        return tf

    @objc.python_method
    def _eyebrow(self, frame, text, upper=True):
        """Small tracked monospace caption: ( LIKE THIS )"""
        tf = AppKit.NSTextField.labelWithAttributedString_(
            _tracked(f"( {text.upper() if upper else text} )", _mono(10.5), INK_SOFT)
        )
        tf.setFrame_(frame)
        tf.setSelectable_(False)
        return tf

    @objc.python_method
    def _field(self, frame, value, placeholder):
        tf = AppKit.NSTextField.alloc().initWithFrame_(frame)
        tf.setBezelStyle_(AppKit.NSTextFieldRoundedBezel)
        tf.setFont_(AppKit.NSFont.systemFontOfSize_(13))
        tf.setStringValue_(value)
        tf.setPlaceholderString_(placeholder)
        return tf

    @objc.python_method
    def _popup(self, frame):
        popup = AppKit.NSPopUpButton.alloc().initWithFrame_pullsDown_(frame, False)
        popup.setFont_(AppKit.NSFont.systemFontOfSize_(13))
        return popup

    @objc.python_method
    def _pill(self, frame, title, action, fill=None, border=None, text_color=INK, mono=False, key=None):
        btn = AppKit.NSButton.alloc().initWithFrame_(frame)
        btn.setBordered_(False)
        btn.setWantsLayer_(True)
        btn.layer().setCornerRadius_(frame.size.height / 2)
        if fill is not None:
            btn.layer().setBackgroundColor_(fill.CGColor())
        if border is not None:
            btn.layer().setBorderWidth_(1.5)
            btn.layer().setBorderColor_(border.CGColor())
        btn.setTarget_(self)
        btn.setAction_(action)
        if key:
            btn.setKeyEquivalent_(key)
        self._set_pill_title(btn, title, text_color, mono)
        return btn

    @objc.python_method
    def _set_pill_title(self, btn, title, color, mono=False):
        if mono:
            btn.setAttributedTitle_(_tracked(title, _mono(10.5), color))
        else:
            font = AppKit.NSFont.systemFontOfSize_weight_(13, AppKit.NSFontWeightSemibold)
            btn.setAttributedTitle_(_tracked(title, font, color, kern=0.2))


# ---------------------------------------------------------------------------
# Status bar (menubar) app
# ---------------------------------------------------------------------------
class BlabApp:

    def __init__(self, config):
        self.config = config
        self.recorder = AudioRecorder(config.get("input_device", DEFAULT_INPUT_DEVICE))
        self.recording = False
        self.processing = False
        self.monitor = None
        self.local_monitor = None
        self._prefs_controller = PreferencesWindowController.alloc().init()

        self.app = AppKit.NSApplication.sharedApplication()
        self.app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)

        # Keep a strong reference — NSApplication.delegate is a weak reference,
        # so without this the delegate would be deallocated immediately.
        self._app_delegate = AppDelegate.alloc().init()
        self.app.setDelegate_(self._app_delegate)

        self.status_item = AppKit.NSStatusBar.systemStatusBar().statusItemWithLength_(
            AppKit.NSVariableStatusItemLength
        )
        self.set_icon("idle")

        menu = AppKit.NSMenu.alloc().init()

        status_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Blab — Ready", None, ""
        )
        status_item.setEnabled_(False)
        menu.addItem_(status_item)
        self.menu_status = status_item

        menu.addItem_(AppKit.NSMenuItem.separatorItem())

        # Preferences… (Cmd+,)
        self._menu_action_helper = MenuActionHelper.alloc().init()
        self._menu_action_helper._callback = self._show_preferences
        prefs_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Preferences\u2026", "showPreferences:", ","
        )
        prefs_item.setTarget_(self._menu_action_helper)
        prefs_item.setEnabled_(True)
        menu.addItem_(prefs_item)
        log.info("Preferences menu item added")

        menu.addItem_(AppKit.NSMenuItem.separatorItem())

        quit_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Quit", "terminate:", "q"
        )
        menu.addItem_(quit_item)

        self.status_item.setMenu_(menu)

        self._main_queue = queue.Queue()
        self._timer_helper = TimerHelper.alloc().init()
        self._timer_helper._drain_fn = self._drain_main_queue
        Foundation.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.05, self._timer_helper, b"fire:", None, True
        )

        self._setup_edit_menu()
        self._register_hotkey()
        log.info(f"App initialized. Hold keycode {config['hotkey_keycode']} to record.")

        self._check_accessibility()
        self._check_api_key()

        self.recorder.prepare()

    def _setup_edit_menu(self):
        """Add a minimal Edit menu to the application main menu.

        macOS routes Cmd+V / Cmd+C / Cmd+A through menu item key equivalents
        before the responder chain.  Without an Edit menu these shortcuts do
        nothing in text fields even when the field has focus.
        """
        main_menu = AppKit.NSMenu.alloc().init()

        edit_item = AppKit.NSMenuItem.alloc().init()
        edit_menu = AppKit.NSMenu.alloc().initWithTitle_("Edit")
        for title, action, key in [
            ("Cut",        "cut:",       "x"),
            ("Copy",       "copy:",      "c"),
            ("Paste",      "paste:",     "v"),
            ("Select All", "selectAll:", "a"),
        ]:
            edit_menu.addItem_(
                AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                    title, action, key
                )
            )
        edit_item.setSubmenu_(edit_menu)
        main_menu.addItem_(edit_item)

        self.app.setMainMenu_(main_menu)

    def _check_accessibility(self):
        """Check permission and, if missing, queue an alert for after app.run().

        NSAlert.runModal() must NOT be called from __init__ — doing so creates a
        nested run loop before app.run() has started, which corrupts PyObjC's FFI
        closure pointers and causes a PAC crash on the next ObjC→Python callback.
        """
        try:
            import ctypes
            axlib = ctypes.CDLL(
                "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
            )
            axlib.AXIsProcessTrusted.restype = ctypes.c_bool
            if axlib.AXIsProcessTrusted():
                log.info("Accessibility permission: GRANTED")
                return

            log.warning("Accessibility permission NOT granted -- paste will not work.")
            # Defer the alert so it fires after app.run() has initialised the loop.
            self._perform_on_main(self._show_accessibility_alert)
        except Exception as e:
            log.warning(f"Could not check accessibility permission: {e}")

    def _check_api_key(self):
        if not keychain_get_api_key():
            log.warning("No API key in Keychain — will prompt user")
            self._perform_on_main(self._show_no_api_key_alert)

    def _show_no_api_key_alert(self):
        try:
            alert = AppKit.NSAlert.alloc().init()
            alert.setMessageText_("OpenAI API Key Required")
            alert.setInformativeText_(
                "No API key is configured. Blab cannot transcribe "
                "audio without it.\n\n"
                "Click 'Open Preferences' to add your key."
            )
            alert.addButtonWithTitle_("Open Preferences")
            alert.addButtonWithTitle_("Later")
            alert.setAlertStyle_(AppKit.NSAlertStyleWarning)
            AppKit.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
            response = alert.runModal()
            if response == AppKit.NSAlertFirstButtonReturn:
                self._show_preferences()
        except Exception as e:
            log.warning(f"No-API-key alert error: {e}")

    def _show_accessibility_alert(self):
        try:
            alert = AppKit.NSAlert.alloc().init()
            alert.setMessageText_("Accessibility Permission Required")
            alert.setInformativeText_(
                "Blab needs Accessibility access to paste transcribed "
                "text into other apps.\n\n"
                "Click 'Open Settings' to go to Privacy & Security > Accessibility,"
                " then add Blab. Restart the app afterward."
            )
            alert.addButtonWithTitle_("Open Settings")
            alert.addButtonWithTitle_("Later")
            alert.setAlertStyle_(AppKit.NSAlertStyleWarning)
            AppKit.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
            response = alert.runModal()
            if response == AppKit.NSAlertFirstButtonReturn:
                import subprocess
                subprocess.Popen([
                    "open",
                    "x-apple.systempreferences:com.apple.preference.security"
                    "?Privacy_Accessibility",
                ])
        except Exception as e:
            log.warning(f"Accessibility alert error: {e}")

    _ICON_SYMBOLS = {"idle": "mic.fill", "recording": "waveform", "processing": "hourglass"}
    _ICON_EMOJI = {"idle": "🎙", "recording": "🔴", "processing": "⏳"}

    def _symbol_image(self, state):
        if not hasattr(self, "_icon_cache"):
            self._icon_cache = {}
        if state not in self._icon_cache:
            image = None
            if hasattr(AppKit.NSImage, "imageWithSystemSymbolName_accessibilityDescription_"):
                image = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(
                    self._ICON_SYMBOLS[state], None
                )
                if image is not None:
                    image.setTemplate_(True)
            self._icon_cache[state] = image
        return self._icon_cache[state]

    def set_icon(self, state, bars=""):
        button = self.status_item.button()
        image = self._symbol_image(state)
        if image is None:  # macOS without SF Symbols
            button.setImage_(None)
            button.setTitle_(self._ICON_EMOJI[state] + bars)
            return
        button.setImage_(image)
        button.setTitle_(bars)
        button.setImagePosition_(AppKit.NSImageLeft if bars else AppKit.NSImageOnly)
        button.setContentTintColor_(AppKit.NSColor.systemRedColor() if state == "recording" else None)

    def _show_preferences(self):
        """Open (or bring to front) the Preferences window."""
        if (self._prefs_controller._window is not None
                and self._prefs_controller._window.isVisible()):
            AppKit.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
            self._prefs_controller._window.makeKeyAndOrderFront_(None)
            return

        self._prefs_controller.show_with_config(
            dict(self.config),          # pass a copy so cancel leaves config intact
            self._on_preferences_saved,
        )

    def _on_preferences_saved(self, new_config):
        """Called by PreferencesWindowController after the user clicks Save."""
        self.config = new_config
        self.recorder.input_device = new_config.get("input_device", DEFAULT_INPUT_DEVICE)
        # If a recording is in flight, the prepare() that follows it picks up
        # the new device instead.
        if not self.recording and not self.processing:
            self.recorder.prepare()
        # Remove old event monitors before re-registering with the new keycode
        if self.monitor:
            AppKit.NSEvent.removeMonitor_(self.monitor)
            self.monitor = None
        if self.local_monitor:
            AppKit.NSEvent.removeMonitor_(self.local_monitor)
            self.local_monitor = None
        self._register_hotkey()
        log.info(
            f"Hotkey updated to keycode {new_config['hotkey_keycode']}"
            f" ({keycode_to_name(new_config['hotkey_keycode'])})"
        )

    def _register_hotkey(self):
        keycode = self.config["hotkey_keycode"]
        mask = AppKit.NSEventMaskKeyDown | AppKit.NSEventMaskKeyUp | AppKit.NSEventMaskFlagsChanged

        def handler(event):
            try:
                etype = event.type()

                if etype == AppKit.NSEventTypeFlagsChanged:
                    if event.keyCode() == keycode:
                        flags = event.modifierFlags()
                        modifier_pressed = self._is_modifier_pressed(keycode, flags)
                        if modifier_pressed and not self.recording and not self.processing:
                            self._start_recording()
                        elif not modifier_pressed and self.recording:
                            self._stop_recording()
                    return

                if event.keyCode() == keycode:
                    if etype == AppKit.NSEventTypeKeyDown and not event.isARepeat():
                        if not self.recording and not self.processing:
                            self._start_recording()
                    elif etype == AppKit.NSEventTypeKeyUp:
                        if self.recording:
                            self._stop_recording()
            except Exception as e:
                log.error(f"Error in hotkey handler: {e}", exc_info=True)

        self.monitor = AppKit.NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
            mask, handler
        )
        self.local_monitor = AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
            mask, lambda event: (handler(event), event)[1]
        )
        log.info(f"Hotkey registered: keycode {keycode}")

    def _is_modifier_pressed(self, keycode, flags):
        modifier_map = {
            58: AppKit.NSEventModifierFlagOption,
            61: AppKit.NSEventModifierFlagOption,
            59: AppKit.NSEventModifierFlagControl,
            62: AppKit.NSEventModifierFlagControl,
            56: AppKit.NSEventModifierFlagShift,
            60: AppKit.NSEventModifierFlagShift,
            55: AppKit.NSEventModifierFlagCommand,
            54: AppKit.NSEventModifierFlagCommand,
            63: AppKit.NSEventModifierFlagFunction,
        }
        flag = modifier_map.get(keycode)
        if flag:
            return bool(flags & flag)
        return False

    def _start_recording(self):
        # Don't start recording while the preferences window is capturing a key
        if getattr(self._prefs_controller, "_capturing", False):
            return

        self.recording = True
        self.set_icon("recording")
        self.menu_status.setTitle_("Recording…")

        success = self.recorder.start()
        if not success:
            log.error("Failed to start recording")
            self.recording = False
            self.set_icon("idle")
            self.menu_status.setTitle_("Blab — Mic Error")
            return

        # Play in a background thread after a delay so the BT A2DP→HFP profile
        # switch has time to complete before the sound fires.  On a cold first
        # press the switch can take 400-600 ms; playing immediately races it and
        # loses.  The delay is imperceptible because recording is already running.
        if self.config.get("sound_on_start"):
            def _play_start_sound():
                time.sleep(0.5)
                play_sound("Tink")
            threading.Thread(target=_play_start_sound, daemon=True).start()

    def _stop_recording(self):
        self.recording = False
        self.processing = True
        self.set_icon("processing")
        self.menu_status.setTitle_("Transcribing…")

        if self.config.get("sound_on_stop"):
            play_sound("Pop")
            # Hold briefly so the Pop sound starts playing before recorder.stop()
            # ends the HFP session — without this, BT headsets cut it off.
            time.sleep(0.15)

        filepath, duration = self.recorder.stop()

        if duration < 0.3:
            log.warning(f"Recording too short ({duration:.1f}s), skipping")
            self._reset_ui()
            threading.Thread(target=self._discard_recording, daemon=True).start()
            return

        def process():
            try:
                self.recorder.wait_until_finalized()
                text = transcribe(filepath, self.config)
                self.recorder.cleanup()

                if text:
                    ClipboardPaster.paste(text)
                    log.info(f"Success: pasted {len(text)} chars")
                else:
                    log.warning("Transcription returned empty — nothing to paste")
            except Exception as e:
                log.error(f"Error in process thread: {e}", exc_info=True)
            finally:
                # Reset UI as soon as Cmd+V has fired — the clipboard restore
                # runs in its own background thread so we don't block here.
                self._perform_on_main(self._reset_ui)
                self._perform_on_main(self.recorder.prepare)

        thread = threading.Thread(target=process, daemon=True)
        thread.start()

    def _discard_recording(self):
        self.recorder.wait_until_finalized()
        self.recorder.cleanup()
        self._perform_on_main(self.recorder.prepare)

    def _reset_ui(self):
        self.processing = False
        self.set_icon("idle")
        self.menu_status.setTitle_("Blab — Ready")

    def _perform_on_main(self, fn):
        self._main_queue.put(fn)

    @staticmethod
    def _level_to_bars(level):
        """Convert a normalised level (0.0–1.0) to a 4-segment VU bar string."""
        n_segs = 4
        filled = int(level * n_segs * len(_BAR_CHARS))  # 0 – 32
        result = ""
        for i in range(n_segs):
            seg = max(0, min(len(_BAR_CHARS), filled - i * len(_BAR_CHARS)))
            result += _BAR_CHARS[seg - 1] if seg > 0 else _BAR_CHARS[0]
        return result

    def _drain_main_queue(self):
        # Piggyback audio-level polling on the existing 50 ms drain timer.
        # This avoids a second NSTimer and ObjC class entirely.
        if self.recording:
            try:
                self.set_icon("recording", self._level_to_bars(self.recorder.get_level()))
            except Exception:
                pass  # never let a meter glitch disrupt the run loop

        while not self._main_queue.empty():
            try:
                fn = self._main_queue.get_nowait()
                fn()
            except queue.Empty:
                break
            except Exception as e:
                log.error(f"Error draining main queue: {e}", exc_info=True)

    def run(self):
        signal.signal(signal.SIGINT, lambda *_: self.app.terminate_(None))
        self.app.run()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    log.info("=" * 50)
    log.info("Blab starting")
    log.info(f"Python: {sys.version}")
    log.info(f"Script: {os.path.abspath(__file__)}")
    config = load_config()
    app = BlabApp(config)
    app.run()

if __name__ == "__main__":
    main()
