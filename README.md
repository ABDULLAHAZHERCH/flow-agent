# Flow Batcher (Fork of `kodelyx/flow-agent`)

A lightweight, 1-click local web UI built on top of [kodelyx/flow-agent](https://github.com/kodelyx/flow-agent) for batch-generating images on **Google Flow** with **strict prompt-index numbering** (`001.jpg`, `002.jpg`, `003.jpg`, etc.).

## Features
- **Strict Prompt Numbering:** Every generated image is saved with its exact 1-based line number from your prompt list.
- **Automatic Batch Folders:** Each run creates a fresh timestamped folder inside `numbered_output/` (e.g., `numbered_output/batch_2026-11-21_21-30-00/`) so separate scripts never overwrite each other.
- **Retry Failed:** One-click retry for failed prompts that preserves their original numbering and batch folder.
- **Multi-Model Support:** Choose between Lite (`harbor_seal`), Standard (`narwhal`), Pro (`gem_pix_2`), or Account Default (`Auto`).
- **1-Click Windows App:** Packaged via PyInstaller with zero Python setup required for end users.

---

## Quick Start (Non-Technical Users — Windows)

1. Go to the **[Releases](https://github.com/ABDULLAHAZHERCH/flow-agent/releases)** page and download `FlowBatcher_Bundle.zip`.
2. **Extract** the ZIP folder (do not run from inside the ZIP).
3. **One-time Chrome setup:**
   - Open `chrome://extensions` in Google Chrome.
   - Turn ON **Developer mode** (top-right).
   - Click **Load unpacked** and select the `flow-extension` folder inside the extracted bundle.
4. **Generate images:**
   - Open [https://flow.google.com](https://flow.google.com) in Chrome, sign in, and keep the tab open.
   - Double-click **`FlowBatcher.exe`** (if Windows Defender prompts, click **More info → Run anyway**).
   - Paste one prompt per line, select your model and aspect ratio, and click **Start Generating**.
   - Click **Quit App** when finished.

---

## Running from Source & Building the `.exe`

```bash
git clone [https://github.com/ABDULLAHAZHERCH/flow-agent.git](https://github.com/ABDULLAHAZHERCH/flow-agent.git)
cd flow-agent
python flow_batcher.py
```

To build the standalone Windows executable:

```bash
pip install pyinstaller
pyinstaller --onefile --noconsole --name FlowBatcher flow_batcher.py
```

Move `dist/FlowBatcher.exe` to the project root (next to `bin/`).

---

## Credits & Attribution

Forked from and powered by **[flow-agent](https://github.com/kodelyx/flow-agent)** by **[kodelyx](https://github.com/kodelyx)**. All core Google Flow transport, Chrome extension bridge, and generation binaries (`bin/`) belong to the original project.