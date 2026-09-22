# Blab

Hold a key, speak, release — your words appear wherever your cursor is.

A lightweight macOS menubar app that records your voice, sends it to OpenAI's transcription API, and pastes the result into whatever text field is focused.

---

## Installation

The easiest way to install on macOS is via the DMG:

1. Download `Blab.dmg` from the [latest release](https://github.com/christianbode-cmd/whisper-dictate/releases/latest)
2. Open the DMG and drag **Blab.app** into your **Applications** folder
3. Launch the app from Applications or Spotlight
4. If macOS says the app is damaged or unverified, run:
   ```bash
   xattr -cr "/Applications/Blab.app"
   ```
5. On first launch, grant **Microphone** and **Accessibility** access when prompted — both are required
6. Click the microphone icon in the menubar → **Preferences…** and enter your [OpenAI API key](https://platform.openai.com/api-keys)

---

## Requirements

- macOS 13 (Ventura) or later
- Python 3.9+
- An [OpenAI API key](https://platform.openai.com/api-keys)

---

## Setup

### 1. Clone the repository

```bash
git clone https://github.com/YOUR_USERNAME/whisper-dictate.git
cd whisper-dictate
```

### 2. Add your OpenAI API key

Your API key is stored securely in the macOS Keychain — never in a file on disk. After launching the app for the first time, you will be prompted to enter it. You can also set or update it at any time via the app's Preferences menu (menubar icon → Preferences… → API Key field).

### 3. Build the app

```bash
chmod +x build_app.sh
./build_app.sh
```

This takes 1–2 minutes. It installs dependencies into a local virtual environment and produces `dist/Blab.app`.

### 4. Install

```bash
cp -R "dist/Blab.app" /Applications/
```

### 5. Launch and grant permissions

```bash
open "/Applications/Blab.app"
```

macOS will prompt for **Microphone** and **Accessibility** access. Both are required:

- **Microphone** — to record your voice
- **Accessibility** — to simulate Cmd+V and paste text into other apps

If macOS says the app is damaged, run:

```bash
xattr -cr "/Applications/Blab.app"
```

---

## Usage

1. Look for the microphone icon in your menubar
2. Click into any text field (Slack, email, browser, Notes, etc.)
3. **Hold the hotkey** (default: Left Option ⌥) — the icon becomes a red waveform with a live level meter
4. **Speak naturally**
5. **Release the key** — an hourglass shows while transcribing
6. Transcribed text is pasted into the focused field and the microphone icon returns

---

## Preferences

Click the microphone icon in the menubar → **Preferences…** (or press **Cmd+,**) to change:

- **API Key** — update your OpenAI key without editing files
- **Microphone** — record from a specific input device instead of the system default. With AirPods or other Bluetooth headsets the first half-second of speech is lost while macOS switches Bluetooth profiles; selecting the built-in microphone avoids this
- **Hotkey** — click the button and press any key or modifier to rebind

Changes take effect immediately after clicking Save.

---

## Configuration

`config.json` is excluded from version control (see `.gitignore`). Use `config.example.json` as a template.

The API key is stored in the macOS Keychain (not in `config.json`) and can be managed via Preferences.

| Setting | Default | Description |
|---|---|---|
| `model` | `gpt-transcribe` | Transcription model |
| `input_device` | `default` | Microphone: `default` follows the system input, or a device unique ID (pick it in Preferences) |
| `hotkey_keycode` | `58` | Trigger key (58 = Left Option ⌥) |
| `language` | `en` | Language hint passed to the API |
| `sound_on_start` | `true` | Play a sound when recording starts |
| `sound_on_stop` | `true` | Play a sound when recording stops |

### Hotkey reference

| Key | Code |
|---|---|
| Left Option ⌥ | `58` |
| Right Option ⌥ | `61` |
| Left Control ⌃ | `59` |
| Right Control ⌃ | `62` |
| Fn | `63` |
| F5–F12 | `96`–`111` |

---

## Cost

`gpt-4o-transcribe` is priced at $2.50 per 1M input tokens and $10 per 1M output tokens (audio input, text output). A typical 10-second dictation costs a fraction of a cent.

`gpt-transcribe`, the new default model, costs approximately $0.0045 per minute of audio.

---

## Rebuilding after changes

If you edit `blab.py`:

```bash
./build_app.sh
osascript -e 'quit app "Blab"'
rm -rf "/Applications/Blab.app" && cp -R "dist/Blab.app" /Applications/
open "/Applications/Blab.app"
```

Re-granting Accessibility permission is required each time the binary changes (macOS revokes it automatically).

---

## Release notes

See [CHANGELOG.md](CHANGELOG.md) for what changed in each version.

---

## Debugging

```bash
tail -f ~/Library/Logs/Blab.log
```

Or run directly in a terminal to see output in real time:

```bash
source .venv/bin/activate
python blab.py
```

---

## License

MIT — see [LICENSE](LICENSE).
