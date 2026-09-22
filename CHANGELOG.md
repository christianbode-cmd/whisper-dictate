# Changelog

All notable changes to Whisper Dictate are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow [Semantic Versioning](https://semver.org/).

## [1.2.0] - 2026-09-22

### Changed
- **Renamed to Blab.** "Whisper Dictate" borrowed OpenAI's model name (no longer even the default) and sat in a crowd of MacWhisper / Superwhisper / Whisper Flow. The script is now `blab.py`, the log is `~/Library/Logs/Blab.log`, and the bundle identifier is `io.github.christianbode-cmd.blab` — so macOS asks for Microphone and Accessibility permission again on first launch. The API key stored under the old Keychain item is migrated automatically.
- **Preferences redesigned**: pale-paper window with a transparent title bar, tracked monospace captions over each field, Model/Microphone and Language/Hotkey side by side, and an ink footer bar with the Save pill. The version is shown in the header.
- **Menubar icon** uses SF Symbols — microphone when idle, a red waveform with the live level meter while recording, an hourglass while transcribing — instead of emoji. Falls back to emoji on macOS versions without SF Symbols.
- New app icon in the same paper-and-ink palette.

## [1.1.0] - 2026-09-22

### Added
- **Microphone picker** in Preferences: record from a specific input device instead of the system default. Fixes the lost first half-second of speech with AirPods and other Bluetooth headsets, which was caused by macOS switching Bluetooth profiles (A2DP → HFP) every time recording started. Select the built-in microphone to avoid the switch entirely and keep AirPods in high-quality playback mode.
- `gpt-transcribe` transcription model, now the default.
- `input_device` config option (`default`, or a device unique ID set via Preferences).

### Changed
- Recorder rewritten on `AVCaptureSession` so it can target a specific device (`AVAudioRecorder` always used the system default).
- OpenAI requests now time out after 30 s with one retry; a hung request previously locked the hotkey for the SDK default of 10 minutes.
- The OpenAI client is cached instead of querying the Keychain on every transcription.
- Cost section in the README reflects current token-based pricing for `gpt-4o-transcribe` and per-minute pricing for `gpt-transcribe`.

### Fixed
- Preferences model popup always showed the first model instead of the configured one.
- Preferences closing no longer quits the app.
- Race when pressing the hotkey again before the recorder had re-armed after the previous recording.
- Removed a broken `AppKit` fallback path for simulating Cmd+V; `pyobjc-framework-Quartz` is now a declared dependency (it was already required in practice).
- Build script: works on a fresh checkout without `config.json`, and falls back to an unstyled DMG when `hdiutil convert` fails on recent macOS.

### Removed
- Experimental `gpt-realtime-whisper` / Realtime API transcription path.

## [1.0.1] - 2026-06-01

### Added
- Model and language selectors in Preferences.
- `gpt-realtime-whisper` model option (removed again in 1.1.0).
- Truncated API key display in Preferences (`sk-proj...abcd`).
- Edit menu so Cmd+V / Cmd+C / Cmd+A work in Preferences text fields.

### Changed
- Clipboard restore decoupled from the processing lock so the hotkey is available again sooner after a paste.

### Fixed
- Crash when opening Preferences a second time.
- Paste into the API key field did not work because the accessory-policy app could not become key.

## [1.0.0] - 2026-05-05

### Added
- Initial release: hold-to-record menubar app that transcribes via OpenAI and pastes the result into the focused field.
- API key stored in the macOS Keychain instead of `config.json`.
- Styled DMG installer with drag-to-Applications background.

### Fixed
- Preferences crash caused by `NSAlert.runModal` during app initialisation.

[1.2.0]: https://github.com/christianbode-cmd/blab/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/christianbode-cmd/blab/compare/v1.0.1...v1.1.0
[1.0.1]: https://github.com/christianbode-cmd/blab/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/christianbode-cmd/blab/releases/tag/v1.0.0
