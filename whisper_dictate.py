#!/usr/bin/env python3
"""
Whisper Dictate — hold-to-record voice transcription for macOS.

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
# Logging — writes to ~/Library/Logs/WhisperDictate.log
# Visible even when running as a .app bundle with no terminal
# ---------------------------------------------------------------------------
LOG_DIR = os.path.expanduser("~/Library/Logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_PATH = os.path.join(LOG_DIR, "WhisperDictate.log")

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("WhisperDictate")
log.info(f"Log file: {LOG_PATH}")

# ---------------------------------------------------------------------------
# Dependency check
# ---------------------------------------------------------------------------
REQUIRED = ["objc", "AppKit", "Foundation", "AVFoundation", "openai"]

def check_dependencies():
    missing = []
    for mod in REQUIRED:
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    if missing:
        log.error(f"Missing dependencies: {', '.join(missing)}")
        log.error("Run: pip install pyobjc-framework-Cocoa pyobjc-framework-AVFoundation openai")
        sys.exit(1)

check_dependencies()

import objc
import AppKit
import Foundation
import AVFoundation
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

    p3 = os.path.expanduser("~/whisper-dictate/config.json")
    if os.path.exists(p3):
        return p3

    return p

CONFIG_PATH = find_config_path()
log.info(f"Config path: {CONFIG_PATH}")

DEFAULT_CONFIG = {
    "hotkey_keycode": 58,
    "model": "gpt-4o-transcribe",
    "language": "en",
    "response_format": "text",
    "prompt": "",
    "sound_on_start": True,
    "sound_on_stop": True,
}

# ---------------------------------------------------------------------------
# Keychain — API key storage
# ---------------------------------------------------------------------------
_KC_SERVICE = "WhisperDictate"
_KC_ACCOUNT = "OpenAIAPIKey"

def keychain_get_api_key():
    """Return the stored API key, or None if not set."""
    try:
        result = subprocess.run(
            ["security", "find-generic-password",
             "-s", _KC_SERVICE, "-a", _KC_ACCOUNT, "-w"],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            key = result.stdout.strip()
            return key or None
    except Exception as e:
        log.warning(f"Keychain read error: {e}")
    return None

def keychain_save_api_key(key):
    """Store (or delete) the API key in the macOS Keychain."""
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

    log.info(f"Config loaded. Model: {merged['model']}, Language: {merged['language']}, Keycode: {merged['hotkey_keycode']}")
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
# Audio recorder using AVFoundation
# ---------------------------------------------------------------------------
class AudioRecorder:
    def __init__(self):
        self.recorder = None
        self.filepath = None
        self._start_time = None
        self._prepared = False

    # Recording settings shared between prepare() and start()
    _SETTINGS = {
        AVFoundation.AVFormatIDKey: int(AVFoundation.kAudioFormatLinearPCM),
        AVFoundation.AVSampleRateKey: 16000.0,
        AVFoundation.AVNumberOfChannelsKey: 1,
        AVFoundation.AVLinearPCMBitDepthKey: 16,
        AVFoundation.AVLinearPCMIsFloatKey: False,
    }

    def prepare(self):
        """Pre-warm the audio hardware so start() can call record() instantly.

        Call this once at startup (in a background thread) and again after
        each recording completes.  Doing so activates the audio pipeline for
        the currently-selected input device — including USB and Bluetooth
        headsets — so the first milliseconds of speech are never clipped.
        """
        self._prepared = False
        self.filepath = os.path.join(tempfile.gettempdir(), "whisper_dictate_recording.wav")

        if os.path.exists(self.filepath):
            try:
                os.remove(self.filepath)
            except OSError:
                pass

        url = Foundation.NSURL.fileURLWithPath_(self.filepath)

        self.recorder, error = AVFoundation.AVAudioRecorder.alloc().initWithURL_settings_error_(
            url, self._SETTINGS, None
        )

        if error or not self.recorder:
            log.error(f"prepare(): Failed to init recorder: {error}")
            return False

        if not self.recorder.prepareToRecord():
            log.error("prepare(): prepareToRecord() returned False")
            return False

        self._prepared = True
        log.info("AudioRecorder pre-warmed and ready")
        return True

    def start(self):
        if not self._prepared:
            # Fallback: prepare inline (e.g. if background pre-warm failed)
            log.warning("start() called without prior prepare() — initialising inline")
            if not self.prepare():
                return False

        self.recorder.setMeteringEnabled_(True)

        success = self.recorder.record()
        if success:
            self._start_time = time.time()
            self._prepared = False  # recorder is now active; prepare() needed next time
            log.info("Recording started")
        else:
            log.error("recorder.record() returned False")
        return success

    def get_level(self):
        """Return normalized audio level 0.0 (silence) to 1.0 (maximum)."""
        if not self.recorder or not self.recorder.isRecording():
            return 0.0
        self.recorder.updateMeters()
        db = self.recorder.averagePowerForChannel_(0)
        # AVAudioRecorder reports dB from ~-160 (silence) to 0 (max).
        # Map the -60 dB to 0 dB range → 0.0 to 1.0 (anything quieter reads as 0).
        MIN_DB = -60.0
        clamped = max(MIN_DB, min(0.0, float(db)))
        return (clamped - MIN_DB) / (-MIN_DB)

    def stop(self):
        duration = 0
        if self.recorder:
            if self.recorder.isRecording():
                self.recorder.stop()
            if self._start_time:
                duration = time.time() - self._start_time
        log.info(f"Recording stopped. Duration: {duration:.1f}s, File: {self.filepath}")
        return self.filepath, duration

    def cleanup(self):
        if self.filepath and os.path.exists(self.filepath):
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

        try:
            import Quartz
            event = Quartz.CGEventCreateKeyboardEvent(None, 0x09, True)
            Quartz.CGEventSetFlags(event, Quartz.kCGEventFlagMaskCommand)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)

            event = Quartz.CGEventCreateKeyboardEvent(None, 0x09, False)
            Quartz.CGEventSetFlags(event, Quartz.kCGEventFlagMaskCommand)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
            log.info("Simulated Cmd+V via Quartz")
        except ImportError:
            src = AppKit.CGEventSourceCreate(AppKit.kCGEventSourceStateHIDSystemState)
            cmd_down = AppKit.CGEventCreateKeyboardEvent(src, 0x09, True)
            AppKit.CGEventSetFlags(cmd_down, AppKit.kCGEventFlagMaskCommand)
            AppKit.CGEventPost(AppKit.kCGHIDEventTap, cmd_down)

            cmd_up = AppKit.CGEventCreateKeyboardEvent(src, 0x09, False)
            AppKit.CGEventSetFlags(cmd_up, AppKit.kCGEventFlagMaskCommand)
            AppKit.CGEventPost(AppKit.kCGHIDEventTap, cmd_up)
            log.info("Simulated Cmd+V via AppKit")

        # Wait long enough for the target app to process the Cmd+V.
        # Apps like Slack and Electron-based editors can be slow to handle paste.
        time.sleep(1.0)

        pb.clearContents()
        if old_string:
            pb.setString_forType_(old_string, AppKit.NSPasteboardTypeString)
            log.debug("Clipboard restored")

# ---------------------------------------------------------------------------
# Transcription
# ---------------------------------------------------------------------------
def transcribe(filepath, config):
    api_key = keychain_get_api_key()
    if not api_key:
        log.error("No API key in Keychain — open Preferences to add one")
        return None

    if not os.path.exists(filepath):
        log.error(f"Recording file not found: {filepath}")
        return None

    file_size = os.path.getsize(filepath)
    log.info(f"Sending to API. Model: {config['model']}, File size: {file_size} bytes")

    if file_size < 1000:
        log.warning(f"Recording file very small ({file_size} bytes) — may be empty/silent")

    client = OpenAI(api_key=api_key)

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
# Preferences window — hotkey capture UI
# ---------------------------------------------------------------------------
class PreferencesWindowController(AppKit.NSObject):
    """
    Native macOS preferences panel.

    Call show_with_config(config, on_save) to display it.
    When the user clicks Save the new config is written to disk and
    on_save(new_config) is called so the app can re-register the hotkey.
    """

    # ------------------------------------------------------------------
    # ObjC action methods (called by NSButton)
    # ------------------------------------------------------------------

    def startCapture_(self, sender):
        """Enter key-capture mode: next key/modifier press becomes the hotkey."""
        if self._capturing:
            return
        self._capturing = True
        self._hotkey_btn.setTitle_("Press a key…")
        self._hotkey_btn.setEnabled_(False)
        self._hint_label.setStringValue_(
            "Press the key or modifier you want to use. Press Esc to cancel."
        )

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
        api_key = self._api_key_field.stringValue().strip()
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
        """Ensure capture monitor is removed if window is closed via the X button."""
        self._cleanup_capture()

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

        WIN_W, WIN_H = 440, 220
        # NSWindowStyleMask: Titled=1, Closable=2, Miniaturizable=4
        WIN_STYLE = 1 | 2 | 4

        self._window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            Foundation.NSMakeRect(0, 0, WIN_W, WIN_H),
            WIN_STYLE,
            AppKit.NSBackingStoreBuffered,
            False,
        )
        self._window.setTitle_("Whisper Dictate — Preferences")
        self._window.setDelegate_(self)
        self._window.center()

        content = self._window.contentView()

        # "API Key:" label + text field
        content.addSubview_(self._make_label(Foundation.NSMakeRect(20, 165, 80, 22), "API Key:"))
        self._api_key_field = AppKit.NSTextField.alloc().initWithFrame_(
            Foundation.NSMakeRect(108, 162, 312, 24)
        )
        self._api_key_field.setStringValue_(keychain_get_api_key() or "")
        self._api_key_field.setPlaceholderString_("sk-...")
        content.addSubview_(self._api_key_field)

        # "Hotkey:" label
        content.addSubview_(self._make_label(Foundation.NSMakeRect(20, 110, 80, 22), "Hotkey:"))

        # Hotkey capture button — shows current key name; click to capture
        self._hotkey_btn = AppKit.NSButton.alloc().initWithFrame_(
            Foundation.NSMakeRect(108, 105, 240, 30)
        )
        self._hotkey_btn.setBezelStyle_(1)  # NSRoundedBezelStyle
        self._hotkey_btn.setTitle_(keycode_to_name(self._pending_keycode))
        self._hotkey_btn.setTarget_(self)
        self._hotkey_btn.setAction_(b"startCapture:")
        content.addSubview_(self._hotkey_btn)

        # Hint / instruction text beneath button
        self._hint_label = self._make_label(
            Foundation.NSMakeRect(108, 78, 316, 22),
            "Click the button above, then press a key or modifier key.",
            small=True,
        )
        content.addSubview_(self._hint_label)

        # Horizontal separator
        sep = AppKit.NSBox.alloc().initWithFrame_(Foundation.NSMakeRect(0, 60, WIN_W, 5))
        sep.setBoxType_(2)  # NSBoxSeparator
        content.addSubview_(sep)

        # Cancel button  (Esc key equivalent)
        cancel_btn = AppKit.NSButton.alloc().initWithFrame_(
            Foundation.NSMakeRect(240, 15, 90, 30)
        )
        cancel_btn.setBezelStyle_(1)
        cancel_btn.setTitle_("Cancel")
        cancel_btn.setTarget_(self)
        cancel_btn.setAction_(b"cancelPrefs:")
        cancel_btn.setKeyEquivalent_("\x1b")  # Escape
        content.addSubview_(cancel_btn)

        # Save button  (Return key equivalent — default button)
        save_btn = AppKit.NSButton.alloc().initWithFrame_(
            Foundation.NSMakeRect(345, 15, 80, 30)
        )
        save_btn.setBezelStyle_(1)
        save_btn.setTitle_("Save")
        save_btn.setTarget_(self)
        save_btn.setAction_(b"savePrefs:")
        save_btn.setKeyEquivalent_("\r")  # Return
        content.addSubview_(save_btn)

        # Bring window to front (works even for accessory-policy apps)
        AppKit.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        self._window.makeKeyAndOrderFront_(None)
        log.info("Preferences window opened")

    def _finish_capture(self, keycode):
        self._pending_keycode = keycode
        self._capturing = False
        if self._capture_monitor:
            AppKit.NSEvent.removeMonitor_(self._capture_monitor)
            self._capture_monitor = None
        self._hotkey_btn.setTitle_(keycode_to_name(keycode))
        self._hotkey_btn.setEnabled_(True)
        self._hint_label.setStringValue_(
            "Click the button above, then press a key or modifier key."
        )
        log.debug(f"Hotkey captured: {keycode} ({keycode_to_name(keycode)})")

    def _cancel_capture(self):
        self._capturing = False
        if self._capture_monitor:
            AppKit.NSEvent.removeMonitor_(self._capture_monitor)
            self._capture_monitor = None
        self._hotkey_btn.setTitle_(keycode_to_name(self._pending_keycode))
        self._hotkey_btn.setEnabled_(True)
        self._hint_label.setStringValue_(
            "Click the button above, then press a key or modifier key."
        )

    def _cleanup_capture(self):
        if self._capture_monitor:
            AppKit.NSEvent.removeMonitor_(self._capture_monitor)
            self._capture_monitor = None
        self._capturing = False

    def _make_label(self, frame, text, small=False):
        tf = AppKit.NSTextField.alloc().initWithFrame_(frame)
        tf.setStringValue_(text)
        tf.setBezeled_(False)
        tf.setDrawsBackground_(False)
        tf.setEditable_(False)
        tf.setSelectable_(False)
        if small:
            tf.setFont_(AppKit.NSFont.systemFontOfSize_(11))
            try:
                tf.setTextColor_(AppKit.NSColor.secondaryLabelColor())
            except AttributeError:
                tf.setTextColor_(AppKit.NSColor.grayColor())
        return tf


# ---------------------------------------------------------------------------
# Status bar (menubar) app
# ---------------------------------------------------------------------------
class WhisperDictateApp:

    def __init__(self, config):
        self.config = config
        self.recorder = AudioRecorder()
        self.recording = False
        self.processing = False
        self.monitor = None
        self.local_monitor = None
        self._prefs_controller = None

        self.app = AppKit.NSApplication.sharedApplication()
        self.app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)

        self.status_item = AppKit.NSStatusBar.systemStatusBar().statusItemWithLength_(
            AppKit.NSVariableStatusItemLength
        )
        self.set_icon("idle")

        menu = AppKit.NSMenu.alloc().init()

        status_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Whisper Dictate — Ready", None, ""
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

        self._register_hotkey()
        log.info(f"App initialized. Hold keycode {config['hotkey_keycode']} to record.")

        self._check_accessibility()
        self._check_api_key()

        # Pre-warm the audio pipeline in the background so the first recording
        # on any input device (especially headsets) starts without a delay.
        threading.Thread(target=self.recorder.prepare, daemon=True).start()

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
                "No API key is configured. Whisper Dictate cannot transcribe "
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
                "Whisper Dictate needs Accessibility access to paste transcribed "
                "text into other apps.\n\n"
                "Click 'Open Settings' to go to Privacy & Security > Accessibility,"
                " then add Whisper Dictate. Restart the app afterward."
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

    def set_icon(self, state):
        button = self.status_item.button()
        if state == "idle":
            button.setTitle_("🎙")
        elif state == "recording":
            button.setTitle_("🔴")
        elif state == "processing":
            button.setTitle_("⏳")

    def _show_preferences(self):
        """Open (or bring to front) the Preferences window."""
        if (self._prefs_controller is not None
                and hasattr(self._prefs_controller, "_window")
                and self._prefs_controller._window is not None
                and self._prefs_controller._window.isVisible()):
            AppKit.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
            self._prefs_controller._window.makeKeyAndOrderFront_(None)
            return

        self._prefs_controller = PreferencesWindowController.alloc().init()
        self._prefs_controller.show_with_config(
            dict(self.config),          # pass a copy so cancel leaves config intact
            self._on_preferences_saved,
        )

    def _on_preferences_saved(self, new_config):
        """Called by PreferencesWindowController after the user clicks Save."""
        self.config = new_config
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
            self.menu_status.setTitle_("Whisper Dictate — Mic Error")
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
            self.recorder.cleanup()
            self._reset_ui()
            threading.Thread(target=self.recorder.prepare, daemon=True).start()
            return

        def process():
            try:
                text = transcribe(filepath, self.config)
                self.recorder.cleanup()

                if text:
                    # Run paste here in the background thread — NSPasteboard and
                    # CGEventPost are both thread-safe, and keeping the 1-second
                    # clipboard-restore sleep off the main thread prevents the
                    # NSRunLoop from freezing (which was delaying hotkey events).
                    ClipboardPaster.paste(text)
                    log.info(f"Success: pasted {len(text)} chars")
                else:
                    log.warning("Transcription returned empty — nothing to paste")
            except Exception as e:
                log.error(f"Error in process thread: {e}", exc_info=True)
            finally:
                self._perform_on_main(self._reset_ui)
                # Re-warm the recorder so the next hotkey press is instant.
                # Runs after cleanup() so the temp file is gone before prepare()
                # creates the new one.
                threading.Thread(target=self.recorder.prepare, daemon=True).start()

        thread = threading.Thread(target=process, daemon=True)
        thread.start()

    def _reset_ui(self):
        self.processing = False
        self.set_icon("idle")
        self.menu_status.setTitle_("Whisper Dictate — Ready")

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
                level = self.recorder.get_level()
                self.status_item.button().setTitle_(f"🔴{self._level_to_bars(level)}")
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
    log.info("Whisper Dictate starting")
    log.info(f"Python: {sys.version}")
    log.info(f"Script: {os.path.abspath(__file__)}")
    config = load_config()
    app = WhisperDictateApp(config)
    app.run()

if __name__ == "__main__":
    main()
