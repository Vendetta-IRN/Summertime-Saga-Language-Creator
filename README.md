<h1 align="center">Summertime Saga Language Creator</h1>

<p align="center">A one-file GUI tool that creates new translation languages for Summertime Saga, extracts its hidden dialogue straight from the game's .rpa archives, and adds an in-game language switcher.</p>

<p align="center">
  <a href="README_fa.md"><img src="https://img.shields.io/badge/README-%D9%81%D8%A7%D8%B1%D8%B3%DB%8C-blue" alt="Persian README"></a>
  <img src="https://img.shields.io/badge/license-MIT-green" alt="MIT License">
</p>

## About The Project

Summertime Saga ships its scripts compiled (`.rpyc`) inside `.rpa` archives such as `scripts.rpa`, and it does not include the original `.rpy` source files. Because of this, the Ren'Py launcher cannot generate translation files for most of the game's text. Many lines, such as the intro "Previously on Summertime Saga", never show up in the standard translation files.

Summertime Saga Language Creator closes that gap. It reads the archives directly, finds the missing texts, and writes translation files that Ren'Py accepts, so you can translate the whole game into any language.

## Key Features

- **Single-File Application**: the whole toolkit is one program. No Python scripts to juggle and no command line needed.
- **Auto-Detection of settings**: pick the game folder and the tool finds your existing languages. It fills in the display name, text direction (RTL/LTR) and font from the files it generated earlier.
- **Smart Extraction (RPA)**: reads `scripts.rpa` (read-only), pulls readable texts out of the compiled scripts, and writes only the ones missing from your translation files.
- **Zero Syntax Errors (strict 4-space indentation)**: every generated `.rpy` file is validated before it is written. It uses 4 spaces and never tabs, which avoids the "Parsing the script failed" error.
- **Duplicate Protection**: before writing, the whole `game/tl/<language>/` folder is scanned. A string that already has an `old "..."` entry is never written again, which prevents the "A translation for ... already exists" crash.
- **In-Game Menu**: adds a language picker to the game (default hotkey Shift+L, optional corner button) that switches language and font, including RTL.

## Download & Installation

1. Open the **[Releases](../../releases)** section of this repository.
2. Download the latest `Summertime-Saga-Language-Creator-vX.Y.Z.exe` (the file name carries the version number). A `.sha256.txt` checksum file is attached to every release.
3. Run it. No installation is needed.

> [!WARNING]
> **Windows SmartScreen may show "Windows protected your PC".**
> The `.exe` is open source and **not code-signed**, so Windows does not recognize the publisher. This is normal for unsigned programs. To run it, click **More info**, then **Run anyway**. If you prefer not to trust a prebuilt file, read the source code and build the `.exe` yourself (see [Build from Source](#build-from-source)).

## Usage Guide

Start by selecting the game's root folder (the one that contains `game/` and `renpy/`). Then choose an existing language or type a new name, for example `persian`.

1. **Language**: creates or updates a language. Set the display name, text direction (RTL/LTR) and font file. The tool writes the language files into the game and queues the translation skeleton. Start the game once and quit, so the game generates the standard translation files.
2. **In-Game Menu**: installs or removes the in-game language picker. Choose the hotkey and an optional corner button.
3. **Extract RPA**: click **Extract RPA & Generate Strings**. The tool reads the game's archives and writes the missing texts to `game/tl/<language>/zz_extracted_dialogues.rpy`, each with an empty `new ""` for you to translate.
4. **Custom Text**: search for an English phrase in your translation files or in the whole `game/` folder. Select results and click **Add to Custom Translation** to queue them in `game/tl/<language>/zz_custom_ui.rpy`. Double-click a result to open its file.

Notes:
- Files the tool overwrites are copied to `_lang_backup/` first. Original game files and `.rpa` archives are never modified.
- Text drawn inside images or videos cannot be found by any text search.
- RTL rendering depends on the font. In this Ren'Py version the font must contain Arabic Presentation Forms for letters to join correctly.

## Build from Source

Requirements: Python 3.8 or newer with tkinter, plus PyInstaller and Pillow. Pillow is needed so that PyInstaller can convert your `icon.png` to an `.ico` for the executable.

Put `icon.png` next to the script, then run:

    pip install pyinstaller pillow
    pyinstaller --noconfirm --clean --onefile --noconsole --name "Summertime Saga Language Creator" --icon icon.png --add-data "icon.png;." summertime_saga_language_creator.py

The finished file is created in the `dist` folder. `--noconsole` hides the console window.

## Credits

Developed by **Vendetta IRN**.

## License

This project is licensed under the **MIT License**. See the `LICENSE` file for details.
