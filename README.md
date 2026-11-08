```markdown
<div align="center">
  <h1>Summertime Saga Language Creator 🛠️</h1>
  <p><strong>An all-in-one GUI tool to create, manage, and extract translations for Summertime Saga (v0.20.16 PC).</strong></p>
  
  <a href="README_fa.md">
    <img src="https://img.shields.io/badge/Language-Persian%20(فارسی)-green?style=for-the-badge" alt="Persian Version">
  </a>
</div>

---

## 📖 About The Project
Because *Summertime Saga* ships with compiled `.rpa` archives and lacks the original `.rpy` source files, standard Ren'Py translation tools fail to add new languages. 

**Summertime Saga Language Creator** bypasses these limitations with a user-friendly, standalone Windows application. It extracts hidden strings directly from the compiled game archives, generates perfectly formatted translation files, and even adds an in-game language switcher.

## ✨ Key Features
* **Single-File Application:** No CLI or terminal needed. Just run the `.exe`.
* **Auto-Detection:** Automatically detects your previously used settings (Font, RTL/LTR, Display Name) when selecting the game folder.
* **Smart Extraction (Step 3):** Bypasses standard Ren'Py limitations to extract hardcoded dialogues and UI text directly from `scripts.rpa`.
* **Zero Syntax Errors:** Generates strictly formatted `.rpy` files (no Tab characters, proper spacing) preventing `Parsing the script failed` crashes.
* **Duplicate Protection:** Scans the entire translation folder before writing to prevent `Duplicate Translation` game crashes.
* **In-Game Menu:** Installs a custom hotkey (`Shift + L`) to switch languages seamlessly in-game without cache issues.

## 📥 Download & Installation
1. Go to the **Releases** section on the right side of this page.
2. Download the latest `Summertime-Saga-Language-Creator-windows.zip`.
3. Extract the `.zip` file.
4. Run `Summertime Saga Language Creator.exe`.

> **⚠️ Windows SmartScreen Warning:** 
> Because this is an open-source modding tool and does not have an expensive digital certificate, Windows might show a warning. This is completely normal. Simply click **"More info"** and then **"Run anyway"**. The source code is 100% open for you to inspect.

## 🚀 Usage Guide

### 1. Language Tab
* Select your game root folder (the folder containing `game/` and `renpy/`).
* Choose your font file *(Highly recommended: **Vazirmatn** for Persian, as it supports Arabic Presentation Forms).*
* Select the text direction (RTL for Persian/Arabic).
* Click **Install Language**. 
* *Note: After this, open the game once and close it. This generates the initial translation files.*

### 2. In-Game Menu Tab
* Use this to install an in-game language picker.
* Click **Install Menu**. Inside the game, press `Shift + L` to switch languages at any time.

### 3. Extract RPA Tab
* Most dialogues are hidden inside the game's `.rpa` archives.
* Click **Extract RPA & Generate Strings**.
* This creates `zz_extracted_dialogues.rpy` in `game/tl/persian/` containing all missing texts.
* Tick "Prefill with English" if you want untranslated lines to show English instead of being blank.

### 4. Custom Text Tab
* Missing a specific UI text (like the intro "Previously on Summertime Saga")?
* Use the search bar to find any string in the game files.
* Click **Add to Custom Translation** to add it to `zz_custom_ui.rpy`.

## 🛠️ Building from Source
If you prefer to run the Python script directly or build the `.exe` yourself:
1. Clone this repository.
2. Ensure you have Python 3.8+ installed.
3. Run the app: `python summertime_saga_language_creator.py`
4. To build the exe:
   ```bash
   pip install pyinstaller pillow
   pyinstaller --noconsole --onefile --icon=icon.png summertime_saga_language_creator.py
```

🙏 Credits

· Tool Developer: Vendetta IRN
· Special Thanks: Summertime Saga modding community.

📜 Disclaimer

This project is for educational and modding purposes only. It is not affiliated with DarkCookie or the official Summertime Saga team. All rights to the game belong to their respective owners.

```
