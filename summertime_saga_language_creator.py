#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Summertime Saga Language Creator
================================
One-file tool that adds a new translation language to a Ren'Py game (made for
Summertime Saga 0.20.x, Ren'Py 7.x) and helps you fill it.

  Tab 1  Language      writes the language files into <game>/game (core, generator,
                       language definition, font copy). Auto-detects existing languages.
  Tab 2  In-game menu  writes the in-game language picker (hotkey / corner button).
  Tab 3  Extract RPA   reads scripts.rpa (read-only, in memory), pulls every readable
                       text out of the compiled .rpyc files and writes the ones missing
                       from your translation files to tl/<lang>/zz_extracted_dialogues.rpy.
  Tab 4  Custom text   search old "..." strings (or any quoted string) and queue them in
                       tl/<lang>/zz_custom_ui.rpy.

Rules every writer in this file follows
  * generated .rpy files use 4 spaces, never tabs, LF line ends, UTF-8
  * before writing old "..." entries the WHOLE game/tl/<lang>/ folder is scanned; a
    string that already has an old "..." entry anywhere there is never written again
    (Ren'Py aborts with "A translation for ... already exists" on duplicates)
  * files that get overwritten are copied to <game root>/_lang_backup/<timestamp>/ first
  * original game files and the .rpa archives are only ever read

Standard library only. Python 3.8+.
"""
import datetime
import hashlib
import io
import json
import os
import pickle
import pickletools
import queue
import re
import shutil
import struct
import sys
import threading
import traceback
import zlib

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:  # lets the logic below be imported and tested without a GUI stack
    tk = None

APP_NAME = "Summertime Saga Language Creator"
APP_VERSION = "1.0.0"
CORE_VERSION = 1
RESERVED = {"none", "python", "strings", "style", "default", "renpy"}
CUSTOM_NAME = "zz_custom_ui.rpy"
EXTRACTED_NAME = "zz_extracted_dialogues.rpy"
MIN_LEN = 4          # extracted strings must be LONGER than 3 characters
MAX_RESULTS = 5000   # search result cap


# =====================================================================
# 1. generic helpers
# =====================================================================
def resource_path(name):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def read_text(path):
    """UTF-8 (BOM tolerated, bad bytes replaced), CRLF -> LF. '' if unreadable."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return ""
    return data.decode("utf-8-sig", "replace").replace("\r\n", "\n").replace("\r", "\n")


def detab(text):
    """Leading tabs -> 4 spaces each (any other tab -> 4 spaces)."""
    out = []
    for line in text.split("\n"):
        m = re.match(r"[ \t]*", line)
        lead = m.group(0)
        if "\t" in lead:
            line = lead.replace("\t", "    ") + line[m.end():]
        if "\t" in line:
            line = line.replace("\t", "    ")
        out.append(line)
    return "\n".join(out)


def strict_rpy(text, label="file"):
    """Final guard for every generated .rpy: no tabs, LF only, every code line indented by
    a multiple of 4 spaces. Raises ValueError instead of writing something Ren'Py would reject."""
    text = detab(text.replace("\r\n", "\n").replace("\r", "\n"))
    for n, line in enumerate(text.split("\n"), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent % 4:
            raise ValueError("%s line %d: indentation %d is not a multiple of 4" % (label, n, indent))
    if not text.endswith("\n"):
        text += "\n"
    return text


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class Backup:
    def __init__(self, root):
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.root = root
        self.dir = os.path.join(root, "_lang_backup", stamp)
        self.items = []

    def save(self, path):
        if not os.path.isfile(path):
            return
        rel = os.path.relpath(path, self.root)
        dst = os.path.join(self.dir, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(path, dst)
        self.items.append(rel)


def write_rpy(path, text, bk, dry):
    """Write a generated .rpy (validated). Returns 'created' | 'updated' | 'unchanged'."""
    text = strict_rpy(text, os.path.basename(path))
    status = "created"
    if os.path.isfile(path):
        with open(path, "rb") as f:
            if f.read() == text.encode("utf-8"):
                return "unchanged"
        status = "updated"
    if dry:
        return status
    if status == "updated":
        bk.save(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    stale = path + "c"  # compiled copy; the engine rebuilds it
    if os.path.isfile(stale):
        os.remove(stale)
    return status


def open_in_editor(path):
    if sys.platform.startswith("win"):
        try:
            os.startfile(path)  # type: ignore[attr-defined]
        except OSError:  # no association for .rpy
            import subprocess
            subprocess.Popen(["notepad.exe", path])
    elif sys.platform == "darwin":
        import subprocess
        subprocess.Popen(["open", "-t", path])
    else:
        import subprocess
        subprocess.Popen(["xdg-open", path])


def make_code(name):
    code = re.sub(r"[^a-z0-9_]+", "_", name.strip().lower()).strip("_")
    if not code or not re.match(r"[a-z]", code):
        raise ValueError("Use an ASCII language name such as 'persian' (the native name goes in "
                         "'Display name'). Got: %r" % name)
    if code in RESERVED:
        raise ValueError("%r is reserved by Ren'Py - pick another name." % code)
    return code


def resolve_root(p):
    """-> (root, game_dir). Accepts the root or its game/ folder."""
    p = os.path.abspath(os.path.expanduser((p or "").strip().strip('"')))
    if os.path.basename(p).lower() == "game" and os.path.isdir(p):
        p = os.path.dirname(p)
    if not os.path.isdir(os.path.join(p, "game")):
        raise ValueError("No 'game' folder in:\n%s\n\nSelect the folder that contains game/ and renpy/." % p)
    return p, os.path.join(p, "game")


def detect_renpy(root):
    txt = read_text(os.path.join(root, "renpy", "__init__.py"))
    m = re.search(r"version_tuple\s*=\s*\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)", txt)
    return tuple(int(x) for x in m.groups()) if m else None


# =====================================================================
# 2. Ren'Py-side code (written into the game)
# =====================================================================
CORE_RPY = r'''# zz-lang-managed: core v@@VERSION@@
# Generated by Summertime Saga Language Creator. Safe to delete together with the other zz_lang_* files.

init -100 python:
    try:
        zz_languages
    except NameError:
        zz_languages = {}

    def zz_lang_register(code, name, rtl, font, replace_fonts, right_align=False):
        zz_languages[code] = {
            "name": name,
            "rtl": bool(rtl),
            "font": font,
            "replace": list(replace_fonts),
            "align": bool(right_align),
        }

init 900 python:
    zz_font_vars = (
        "text_font", "name_text_font", "interface_text_font",
        "button_text_font", "choice_button_text_font", "label_text_font",
    )

    def zz_lang_snapshot():
        d = {}
        g = getattr(store, "gui", None)
        if g is not None:
            for n in zz_font_vars:
                if hasattr(g, n):
                    d[n] = getattr(g, n)
        return d

    zz_lang_orig = zz_lang_snapshot()
    zz_lang_orig_has_rtl = hasattr(config, "rtl")
    zz_lang_orig_rtl = bool(getattr(config, "rtl", False))
    zz_lang_orig_map = dict(config.font_replacement_map)

    def zz_lang_apply(code=None):
        """Restore original font/RTL state, then apply language `code` (None = original)."""
        g = getattr(store, "gui", None)
        if g is not None:
            for n, v in zz_lang_orig.items():
                try:
                    setattr(g, n, v)
                except Exception:
                    pass
        try:
            config.font_replacement_map.clear()
            config.font_replacement_map.update(zz_lang_orig_map)
        except Exception:
            pass
        if zz_lang_orig_has_rtl:
            try:
                config.rtl = zz_lang_orig_rtl
            except Exception:
                pass

        info = zz_languages.get(code) if code else None
        if info is None:
            return

        f = info["font"]
        if g is not None:
            for n in zz_lang_orig:
                try:
                    setattr(g, n, f)
                except Exception:
                    pass
        for old in info["replace"]:
            for b in (False, True):
                for i in (False, True):
                    try:
                        config.font_replacement_map[(old, b, i)] = (f, b, i)
                    except Exception:
                        pass
        if info["rtl"]:
            try:
                config.rtl = True
            except Exception:
                pass
'''

# Runs inside the game (Python 2.7). Keep it py2-compatible.
GEN_RPY = r'''# zz-lang-managed: generator v@@VERSION@@
# Writes game/tl/<code>/*.rpy for every language folder containing a ".pending" file.
# Never overwrites an existing file; ids that already have a translation and strings that
# already have an old "..." entry anywhere in game/tl/<code>/ are skipped (no duplicates).

init 1000 python:
    import io
    import os
    import re

    def zz_gen_u(s):
        if isinstance(s, bytes):
            return s.decode("utf-8", "replace")
        return s

    def zz_gen_q(s):
        s = zz_gen_u(s)
        return u'"' + s.replace(u"\\", u"\\\\").replace(u'"', u'\\"').replace(u"\n", u"\\n").replace(u"\t", u"\\t") + u'"'

    def zz_gen_block(node):
        out = []
        for n in node.block:
            gc = getattr(n, "get_code", None)
            if gc is None:
                return None
            out.append(zz_gen_u(gc()).replace(u"\t", u"\\t").replace(u"\n", u" "))
        return out or None

    def zz_gen_existing_olds(base):
        found = set()
        rx = re.compile(u'^[ \\t]*old[ \\t]+"((?:[^"\\\\\\n]|\\\\.)*)"', re.M)
        for d, dirs, files in os.walk(base):
            for fn in files:
                if fn.lower().endswith(".rpy"):
                    try:
                        with io.open(os.path.join(d, fn), "r", encoding="utf-8-sig") as fh:
                            for m in rx.finditer(fh.read()):
                                found.add(m.group(1))
                    except Exception:
                        pass
        return found

    def zz_lang_generate(code):
        script = renpy.game.script
        existing = getattr(script.translator, "language_translates", None) or {}
        base = os.path.join(config.gamedir, "tl", code)
        if not os.path.isdir(base):
            os.makedirs(base)

        items = []
        menus = []
        for node in list(script.namemap.values()):
            if isinstance(node, renpy.ast.Translate):
                if node.language is not None:
                    continue
                try:
                    if (node.identifier, code) in existing:
                        continue
                except Exception:
                    pass
                items.append(node)
            elif isinstance(node, renpy.ast.Menu):
                menus.append(node)

        items.sort(key=lambda n: (n.filename, n.linenumber))
        menus.sort(key=lambda n: (n.filename, n.linenumber))

        files = {}
        skipped = 0
        count = 0
        for node in items:
            codes = zz_gen_block(node)
            if codes is None:
                skipped += 1
                continue
            fn = zz_gen_u(node.filename)
            key = re.sub(r"[^A-Za-z0-9_]+", "_", os.path.splitext(fn)[0]).strip("_") or "script"
            buf = files.setdefault(key, [])
            buf.append(u"# %s:%d\n" % (fn, node.linenumber))
            buf.append(u"translate %s %s:\n\n" % (code, zz_gen_u(node.identifier)))
            for c in codes:
                buf.append(u"    # %s\n" % c)
            for c in codes:
                buf.append(u"    %s\n" % c)
            buf.append(u"\n")
            count += 1

        written = []
        for key in sorted(files):
            n = 0
            path = os.path.join(base, key + ".rpy")
            while os.path.exists(path):
                n += 1
                path = os.path.join(base, "%s_add%d.rpy" % (key, n))
            with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(u"".join(files[key]))
            written.append(os.path.basename(path))

        strings = 0
        mpath = os.path.join(base, "menu_strings.rpy")
        if menus and not os.path.exists(mpath):
            have = zz_gen_existing_olds(base)
            seen = set()
            lines = []
            for m in menus:
                for it in m.items:
                    label = it[0]
                    if not label:
                        continue
                    q = zz_gen_q(label)
                    if q[1:-1] in have or q in seen:
                        continue
                    seen.add(q)
                    lines.append(u"    # %s:%d\n    old %s\n    new %s\n\n" % (
                        zz_gen_u(m.filename), m.linenumber, q, q))
            if lines:
                with io.open(mpath, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(u"translate %s strings:\n\n" % code + u"".join(lines))
                strings = len(lines)
                written.append("menu_strings.rpy")

        log = u"dialogue blocks: %d\nmenu strings: %d\nskipped blocks: %d\nfiles: %s\n" % (
            count, strings, skipped, u", ".join(written))
        with io.open(os.path.join(base, "_generated.txt"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(log)
        return log

    def zz_lang_auto_generate():
        tl = os.path.join(config.gamedir, "tl")
        if not os.path.isdir(tl):
            return
        for code in sorted(os.listdir(tl)):
            pending = os.path.join(tl, code, ".pending")
            if not os.path.isfile(pending):
                continue
            try:
                zz_lang_generate(code)
                os.remove(pending)
            except Exception as e:
                try:
                    with io.open(os.path.join(config.basedir, "zz_lang_error.txt"), "a", encoding="utf-8") as fh:
                        fh.write(u"generate %s failed: %r\n" % (code, e))
                except Exception:
                    pass

    zz_lang_auto_generate()
'''

MENU_RPY = r'''# zz-lang-managed: menu v@@VERSION@@
# Generated by Summertime Saga Language Creator. Safe to delete (or use "Remove menu").

init 901 python:
    def zz_lang_registry():
        return getattr(store, "zz_languages", {})

    def zz_lang_base_font():
        f = getattr(store, "zz_lang_orig", {}).get("interface_text_font")
        if not f:
            try:
                f = style.default.font
            except Exception:
                f = None
        return f or "DejaVuSans.ttf"

    zz_lang_default_font = zz_lang_base_font()

    def zz_lang_list():
        codes = set(zz_lang_registry().keys())
        try:
            codes.update(renpy.known_languages())
        except Exception:
            pass
        return sorted(codes)

    def zz_lang_label(code):
        info = zz_lang_registry().get(code)
        if info:
            return info["name"]
        return code.capitalize()

    def zz_lang_font(code):
        info = zz_lang_registry().get(code)
        return info["font"] if info else zz_lang_default_font

    def zz_lang_current():
        return renpy.game.preferences.language

    def zz_lang_refresh():
        """Rebuild styles and drop caches so the new fonts really take effect."""
        g = getattr(store, "gui", None)
        done = False
        if g is not None and hasattr(g, "rebuild"):
            try:
                g.rebuild()
                done = True
            except Exception:
                done = False
        if not done:
            try:
                renpy.style.rebuild()
            except Exception:
                pass
        try:
            renpy.text.text.layout_cache_clear()
        except Exception:
            pass
        try:
            renpy.free_memory()
        except Exception:
            pass

    def zz_lang_switch(code):
        if code == "":
            code = None
        known = set()
        try:
            known = set(renpy.known_languages())
        except Exception:
            pass
        if code is not None and code not in known and code not in zz_lang_registry():
            renpy.notify("Unknown language: %s" % code)
            return
        # 1. drop whatever the previous language set (fonts, RTL, replacement map)
        if hasattr(store, "zz_lang_apply"):
            zz_lang_apply(None)
        # 2. switch (runs the target language's translate python/style blocks)
        try:
            renpy.change_language(code, force=True)
        except TypeError:
            renpy.change_language(code)
        # 3. caches
        zz_lang_refresh()
        zz_lang_sanity.seen = code
        # 4. persist now, not only on quit
        try:
            renpy.save_persistent()
        except Exception:
            pass
        renpy.restart_interaction()

    def zz_lang_sanity():
        """Runs before every interaction. Cheap. Repairs stale/invalid language state."""
        cur = zz_lang_current()
        seen = getattr(zz_lang_sanity, "seen", "<unset>")
        if cur == seen:
            return
        zz_lang_sanity.seen = cur
        known = set(zz_lang_registry().keys())
        try:
            known.update(renpy.known_languages())
        except Exception:
            pass
        if seen == "<unset>":
            # first interaction after start: saved language may point at something that is gone
            if cur is not None and cur not in known:
                try:
                    renpy.change_language(None)
                except Exception:
                    pass
                if hasattr(store, "zz_lang_apply"):
                    zz_lang_apply(None)
                zz_lang_refresh()
                zz_lang_sanity.seen = None
            return
        # language was changed by something other than our menu
        if cur not in zz_lang_registry() and hasattr(store, "zz_lang_apply"):
            zz_lang_apply(None)
            zz_lang_refresh()

    config.interact_callbacks.append(zz_lang_sanity)
    config.underlay.append(renpy.Keymap(**{"@@HOTKEY@@": Show("zz_language_menu")}))
@@OVERLAY@@
screen zz_language_menu():
    modal True
    zorder 300
    key "game_menu" action Hide("zz_language_menu")
    add Solid("#000000b0")
    frame:
        align (0.5, 0.5)
        padding (40, 30)
        xminimum 480
        vbox:
            spacing 10
            text "Language" size 32 xalign 0.5 font zz_lang_default_font
            null height 6
            textbutton "Default":
                xalign 0.5
                text_font zz_lang_default_font
                selected (zz_lang_current() is None)
                action Function(zz_lang_switch, None)
            for code in zz_lang_list():
                textbutton zz_lang_label(code):
                    xalign 0.5
                    text_font zz_lang_font(code)
                    selected (zz_lang_current() == code)
                    action Function(zz_lang_switch, code)
            null height 6
            textbutton "Close":
                xalign 0.5
                text_font zz_lang_default_font
                action Hide("zz_language_menu")
'''

OVERLAY_RPY = r'''
init python:
    config.overlay_screens.append("zz_lang_button")

screen zz_lang_button():
    zorder 100
    textbutton "Language":
        align (@@X@@, @@Y@@)
        text_size 18
        action Show("zz_language_menu")
'''

BUTTON_POS = {"tl": ("0.0", "0.0"), "tr": ("1.0", "0.0"), "bl": ("0.0", "1.0"), "br": ("1.0", "1.0")}


def language_rpy(code, display, rtl, font_rel, replace, right_align):
    out = [
        "# zz-lang-managed: language %s" % code,
        "# Generated by Summertime Saga Language Creator. Edit freely; installing again restores it.",
        "init -50 python:",
        "    zz_lang_register(%s, u%s, %s, %s, %s, %s)" % (
            json.dumps(code), json.dumps(display), bool(rtl), json.dumps(font_rel),
            json.dumps(list(replace)), bool(right_align)),
        "",
        "translate %s style default:" % code,
        "    font %s" % json.dumps(font_rel),
    ]
    if rtl:
        out.append('    language "unicode"')
    if right_align:
        out.append("    text_align 1.0")
    out += ["", "translate %s python:" % code, "    zz_lang_apply(%s)" % json.dumps(code), ""]
    return "\n".join(out)


# =====================================================================
# 3. auto-detection of existing languages
# =====================================================================
_REG_RE = re.compile(
    r'zz_lang_register\(\s*"([^"]+)"\s*,\s*u?("(?:[^"\\]|\\.)*")\s*,\s*(True|False)\s*,\s*'
    r'("(?:[^"\\]|\\.)*")\s*,\s*(\[[^\]]*\])\s*,\s*(True|False)\s*\)')
_OLD_LINE_RE = re.compile(r'^[ \t]*old[ \t]+("(?:[^"\\\n]|\\.)*"|\'(?:[^\'\\\n]|\\.)*\')', re.M)
_FONT_EXTS = (".ttf", ".otf", ".ttc")


def _jstr(lit):
    try:
        return json.loads(lit)
    except ValueError:
        return lit[1:-1]


def list_languages(game_dir):
    """Language ids found in game/tl/ (folders) and in game/zz_lang_*.rpy definitions."""
    langs = set()
    tl = os.path.join(game_dir, "tl")
    if os.path.isdir(tl):
        for n in os.listdir(tl):
            if os.path.isdir(os.path.join(tl, n)) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", n) \
                    and n.lower() != "none":
                langs.add(n)
    try:
        for n in os.listdir(game_dir):
            m = re.fullmatch(r"zz_lang_([a-z][a-z0-9_]*)\.rpy", n)
            if m and m.group(1) not in ("core", "gen", "menu"):
                langs.add(m.group(1))
    except OSError:
        pass
    return sorted(langs)


def detect_language(game_dir, code):
    """Read zz_lang_<code>.rpy (and zz_custom_ui.rpy) -> dict of settings, or None if the
    language has no definition file. Keys: display, rtl, font_rel, font_abs, replace,
    right_align, custom_entries, files."""
    info = {"display": "", "rtl": None, "font_rel": "", "font_abs": "", "replace": [],
            "right_align": False, "custom_entries": 0, "files": [], "source": ""}
    cands = [os.path.join(game_dir, "zz_lang_%s.rpy" % code)]
    try:
        cands += [os.path.join(game_dir, n) for n in sorted(os.listdir(game_dir))
                  if re.fullmatch(r"zz_lang_.+\.rpy", n) and n.lower() != cands[0].lower()]
    except OSError:
        pass
    text = ""
    for path in cands:
        t = read_text(path)
        for m in _REG_RE.finditer(t):
            if m.group(1) == code:
                text = t
                info["source"] = os.path.basename(path)
                info["display"] = _jstr(m.group(2))
                info["rtl"] = m.group(3) == "True"
                info["font_rel"] = _jstr(m.group(4))
                try:
                    info["replace"] = [str(x) for x in json.loads(m.group(5))]
                except ValueError:
                    info["replace"] = []
                info["right_align"] = m.group(6) == "True"
                break
        if info["source"]:
            break
    if not info["source"]:
        return None
    info["files"].append(info["source"])
    # fallbacks / cross-checks from the style block of the same file
    if not info["font_rel"]:
        m = re.search(r'translate\s+%s\s+style\s+default\s*:\s*\n(?:[ ]+.*\n)*?[ ]+font\s+"([^"]+)"' % re.escape(code), text)
        if m:
            info["font_rel"] = m.group(1)
    if info["rtl"] is None:
        info["rtl"] = bool(re.search(r'^[ ]+language\s+"unicode"', text, re.M))
    if not info["font_rel"]:  # last resort: the only font in lang_fonts/
        fd = os.path.join(game_dir, "lang_fonts")
        fonts = [n for n in os.listdir(fd) if n.lower().endswith(_FONT_EXTS)] if os.path.isdir(fd) else []
        if len(fonts) == 1:
            info["font_rel"] = "lang_fonts/" + fonts[0]
    if info["font_rel"]:
        info["font_abs"] = os.path.normpath(os.path.join(game_dir, *info["font_rel"].split("/")))
    custom = os.path.join(game_dir, "tl", code, CUSTOM_NAME)
    if os.path.isfile(custom):
        info["files"].append("tl/%s/%s" % (code, CUSTOM_NAME))
        info["custom_entries"] = len(_OLD_LINE_RE.findall(read_text(custom)))
    return info


# =====================================================================
# 4. language / menu installers (native: no subprocess, no external scripts)
# =====================================================================
def _font_ranges(path):
    with open(path, "rb") as f:
        data = f.read()
    base = 0
    if data[:4] == b"ttcf":
        base = struct.unpack(">I", data[12:16])[0]
    nt = struct.unpack(">H", data[base + 4:base + 6])[0]
    cmap = None
    for i in range(nt):
        e = base + 12 + 16 * i
        tag, _cs, off, _ln = struct.unpack(">4sIII", data[e:e + 16])
        if tag == b"cmap":
            cmap = off
    if cmap is None:
        return None
    n = struct.unpack(">H", data[cmap + 2:cmap + 4])[0]
    ranges = []
    for i in range(n):
        _pid, _eid, so = struct.unpack(">HHI", data[cmap + 4 + 8 * i:cmap + 12 + 8 * i])
        s = cmap + so
        fmt = struct.unpack(">H", data[s:s + 2])[0]
        if fmt == 4:
            segx2 = struct.unpack(">H", data[s + 6:s + 8])[0]
            sc = segx2 // 2
            ends = struct.unpack(">%dH" % sc, data[s + 14:s + 14 + segx2])
            starts = struct.unpack(">%dH" % sc, data[s + 16 + segx2:s + 16 + 2 * segx2])
            ranges += [(a, b) for a, b in zip(starts, ends) if a != 0xFFFF]
        elif fmt == 12:
            ng = struct.unpack(">I", data[s + 12:s + 16])[0]
            for g in range(ng):
                a, b, _ = struct.unpack(">III", data[s + 16 + 12 * g:s + 28 + 12 * g])
                ranges.append((a, b))
    return ranges


def check_font(path, rtl):
    warns = []
    with open(path, "rb") as f:
        magic = f.read(4)
    if magic not in (b"\x00\x01\x00\x00", b"OTTO", b"true", b"ttcf"):
        return ["file does not start like a TrueType/OpenType font - Ren'Py may refuse it"]
    if magic == b"ttcf":
        warns.append(".ttc collection: Ren'Py uses only the first face unless you write 'file.ttc@N'")
    if not rtl:
        return warns
    try:
        ranges = _font_ranges(path)
    except Exception as e:
        return warns + ["could not inspect glyph coverage (%s) - test in game" % e]
    if ranges is None:
        return warns + ["font has no cmap table"]

    def has(cp):
        return any(a <= cp <= b for a, b in ranges)

    base_missing = [c for c in (0x627, 0x628, 0x67E, 0x686, 0x698, 0x6AF, 0x6CC) if not has(c)]
    pres_missing = [c for c in (0xFE8E, 0xFE8F, 0xFE90, 0xFE91, 0xFE92, 0xFB56, 0xFB57, 0xFB58, 0xFB59) if not has(c)]
    if base_missing:
        warns.append("font lacks base Arabic/Persian letters: " + " ".join("U+%04X" % c for c in base_missing))
    if pres_missing:
        warns.append("font lacks Arabic Presentation Forms (%s). This engine joins letters by mapping to those "
                     "glyphs - expect disconnected letters. Pick a font that has U+FB50-FDFF and U+FE70-FEFF."
                     % " ".join("U+%04X" % c for c in pres_missing))
    return warns


def has_skeleton(tl_dir):
    try:
        return any(n.lower().endswith(".rpy") and not n.lower().startswith("zz_") for n in os.listdir(tl_dir))
    except OSError:
        return False


def install_language(root, p, dry, log):
    """p: name, display, direction ('rtl'|'ltr'), font, replace (list), right_align, regenerate.
    Returns True when files were written (or would be, for a dry run)."""
    game = os.path.join(root, "game")
    code = make_code(p["name"])
    rtl = p["direction"] == "rtl"
    display = (p.get("display") or p["name"]).strip()
    font_src = os.path.abspath(os.path.expanduser((p.get("font") or "").strip().strip('"')))
    if not os.path.isfile(font_src):
        raise ValueError("Font file not found:\n%s" % font_src)
    if os.path.splitext(font_src)[1].lower() not in _FONT_EXTS:
        raise ValueError("Font must be .ttf, .otf or .ttc")
    if p.get("right_align") and not rtl:
        raise ValueError("Right-align only makes sense with direction RTL.")
    if not dry and not os.access(game, os.W_OK):
        raise ValueError("No write permission on %s\n(installed under Program Files? copy the game elsewhere)" % game)

    warnings = []
    rv = detect_renpy(root)
    ver = read_text(os.path.join(game, "script_version.txt")).strip()
    log("Game root   : %s\n" % root)
    log("Game version: %s\n" % (ver or "unknown"))
    log("Ren'Py      : %s\n" % (".".join(map(str, rv)) if rv else "unknown"))
    if rv and rv[0] != 7:
        warnings.append("Ren'Py %d.x detected; written for 7.x. Test on a copy." % rv[0])
    if not os.path.isfile(os.path.join(game, "scripts.rpa")):
        warnings.append("game/scripts.rpa not found - layout differs from Summertime Saga 0.20.x")
    warnings += ["font: " + w for w in check_font(font_src, rtl)]
    if rtl:
        warnings.append("RTL: this Ren'Py build reorders RTL text itself and has no HarfBuzz. Letter joining "
                        "depends on the font's Presentation-Forms glyphs. Test in game.")
    if not p.get("replace"):
        warnings.append("no replace-fonts given: only the gui font variables and the default style are switched; "
                        "UI text with a hard-coded font keeps its old font")

    fname = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(font_src))
    font_rel = "lang_fonts/%s" % fname
    font_dst = os.path.join(game, "lang_fonts", fname)
    tl_dir = os.path.join(game, "tl", code)
    queue_gen = bool(p.get("regenerate")) or not has_skeleton(tl_dir)

    log("\nLanguage    : %s (id '%s', %s)\n" % (display, code, "RTL" if rtl else "LTR"))
    log("Font        : %s -> game/%s\n" % (font_src, font_rel))
    log("Skeleton    : %s\n" % ("queued for next game launch" if queue_gen else "exists - not queued (tick 'Regenerate' to add new ids)"))
    for w in warnings:
        log("WARNING     : %s\n" % w)
    log("Backups     : overwritten files go to %s\n" % os.path.join(root, "_lang_backup", "<timestamp>"))
    log("Untouched   : scripts.rpa, images.rpa, data.rpa, saves, persistent data, all original files\n")
    if dry:
        log("\nPreview only - nothing changed.\n")
        return False

    bk = Backup(root)
    results = [
        ("zz_lang_core.rpy", write_rpy(os.path.join(game, "zz_lang_core.rpy"),
                                       CORE_RPY.replace("@@VERSION@@", str(CORE_VERSION)), bk, False)),
        ("zz_lang_gen.rpy", write_rpy(os.path.join(game, "zz_lang_gen.rpy"),
                                      GEN_RPY.replace("@@VERSION@@", str(CORE_VERSION)), bk, False)),
        ("zz_lang_%s.rpy" % code, write_rpy(
            os.path.join(game, "zz_lang_%s.rpy" % code),
            language_rpy(code, display, rtl, font_rel, p.get("replace") or [], p.get("right_align")), bk, False)),
    ]
    same = os.path.isfile(font_dst) and os.path.samefile(font_src, font_dst)
    if same or (os.path.isfile(font_dst) and sha256(font_dst) == sha256(font_src)):
        results.append((font_rel, "unchanged"))
    else:
        existed = os.path.isfile(font_dst)
        bk.save(font_dst)
        os.makedirs(os.path.dirname(font_dst), exist_ok=True)
        shutil.copy2(font_src, font_dst)
        results.append((font_rel, "updated" if existed else "created"))
    os.makedirs(tl_dir, exist_ok=True)
    if queue_gen:
        with open(os.path.join(tl_dir, ".pending"), "w", encoding="utf-8") as f:
            f.write("generator queued by %s\n" % APP_NAME)

    log("\nResult:\n")
    for n, s in results:
        log("  %-10s %s\n" % (s, n))
    if bk.items:
        log("Backed up to %s:\n" % bk.dir)
        for i in bk.items:
            log("   %s\n" % i)
    log("\nNext:\n"
        "  1. Start the game ONCE and quit. The first start is slower: it compiles the new files and\n"
        "     writes game/tl/%(c)s/*.rpy (zz_lang_error.txt appears in the game root if that fails).\n"
        "  2. Tab 3 / Tab 4 add texts the generator cannot see. Then translate: replace the text inside the\n"
        "     quotes under each '# original' line, or fill the empty new \"\" (UTF-8).\n"
        "  3. Tab 2 installs the in-game picker (default hotkey Shift+L).\n"
        "To undo: delete zz_lang_*.rpy(+.rpyc), lang_fonts/ and tl/%(c)s/ from the game folder.\n" % {"c": code})
    return True


def install_menu(root, hotkey, button, dry, log):
    game = os.path.join(root, "game")
    if not re.fullmatch(r"(?:(?:shift|alt|ctrl)_)*K_[A-Za-z0-9_]+", hotkey):
        raise ValueError("Hotkey must look like shift_K_l, ctrl_K_F9 or K_F9.")
    if button not in ("none", "tl", "tr", "bl", "br"):
        raise ValueError("Bad corner button value.")
    overlay = ""
    if button != "none":
        x, y = BUTTON_POS[button]
        overlay = OVERLAY_RPY.replace("@@X@@", x).replace("@@Y@@", y)
    text = (MENU_RPY.replace("@@VERSION@@", str(CORE_VERSION))
            .replace("@@HOTKEY@@", hotkey).replace("@@OVERLAY@@", overlay))
    target = os.path.join(game, "zz_lang_menu.rpy")
    if not os.path.isfile(os.path.join(game, "zz_lang_core.rpy")):
        log("WARNING: zz_lang_core.rpy missing - install a language first (tab 1). The menu would switch "
            "languages but not fonts/RTL.\n")
    log("Hotkey: %s   Corner button: %s\n" % (hotkey, button))
    if dry:
        write_rpy(target, text, Backup(root), True)  # validates the text
        log("Preview only - nothing changed.\n")
        return
    bk = Backup(root)
    status = write_rpy(target, text, bk, False)
    log("zz_lang_menu.rpy: %s\n" % status)
    if bk.items:
        log("Backed up to %s\n" % bk.dir)
    log("Start the game and press the hotkey.\n")


def remove_menu(root, dry, log):
    target = os.path.join(root, "game", "zz_lang_menu.rpy")
    if not os.path.isfile(target):
        log("Nothing to remove.\n")
        return
    if dry:
        log("Would remove %s\n" % target)
        return
    bk = Backup(root)
    bk.save(target)
    os.remove(target)
    if os.path.isfile(target + "c"):
        os.remove(target + "c")
    log("Removed zz_lang_menu.rpy (backup: %s)\n" % bk.dir)


# =====================================================================
# 5. string tables of tl/<lang>/ and duplicate-safe writing
# =====================================================================
_LIT_RE = re.compile(r'''[uUrR]?("(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*')''')
_COMMENT_RE = re.compile(r"^[ \t]*#([^\n]*)", re.M)
_HEADER_RE = re.compile(r"^translate[ \t]+(\S+)[ \t]+strings[ \t]*:[ \t]*(#.*)?$")


def unescape_source(c):
    m = {"n": "\n", "t": "\t"}
    return re.sub(r"\\(.)", lambda x: m.get(x.group(1), x.group(1)), c, flags=re.S)


def quote_rpy(text):
    t = (text.replace("\\", "\\\\").replace('"', '\\"').replace("\r", "")
         .replace("\n", "\\n").replace("\t", "\\t"))
    return '"' + t + '"'


def _rpy_files(base):
    out = []
    for d, dirs, files in os.walk(base):
        dirs.sort()
        for n in sorted(files):
            if n.lower().endswith(".rpy"):
                out.append(os.path.join(d, n))
    return out


def scan_old_texts(tl_dir):
    """{real text: (path, offset)} for every old "..." under tl_dir (whole folder, recursive)."""
    seen = {}
    for path in _rpy_files(tl_dir):
        text = read_text(path)
        for m in _OLD_LINE_RE.finditer(text):
            lit = m.group(1)
            seen.setdefault(unescape_source(lit[1:-1]), (path, m.start()))
    return seen


def scan_comment_texts(tl_dir):
    """Original texts of generated dialogue blocks: the '# who "text"' comment lines."""
    seen = set()
    for path in _rpy_files(tl_dir):
        text = read_text(path)
        for m in _COMMENT_RE.finditer(text):
            if '"' in m.group(1) or "'" in m.group(1):
                for lm in _LIT_RE.finditer(m.group(1)):
                    seen.add(unescape_source(lm.group(1)[1:-1]))
    return seen


def where(loc, root):
    path, off = loc
    line = read_text(path).count("\n", 0, off) + 1
    return "%s:%d" % (os.path.relpath(path, root).replace("\\", "/"), line)


def _ends_in_strings_block(content, lang):
    last = None
    for line in content.split("\n"):
        if line.strip() and not line[0].isspace() and not line.lstrip().startswith("#"):
            last = line.rstrip()
    if not last:
        return False
    m = _HEADER_RE.match(last)
    return bool(m and m.group(1) == lang)


def add_old_entries(root, lang, filename, entries, prefill=False):
    """Append old/new pairs to game/tl/<lang>/<filename>.
    entries: [(text, source_note)]. The whole tl/<lang>/ folder is re-scanned first; a text that
    already has an old "..." entry anywhere there is skipped. Existing content is never rewritten,
    except that tab indentation in the target file is converted to 4 spaces (backup first).
    Returns (written_count, [(text, 'rel/path:line') skipped as duplicates])."""
    tl_dir = os.path.join(root, "game", "tl", lang)
    path = os.path.join(tl_dir, filename)
    existing = scan_old_texts(tl_dir) if os.path.isdir(tl_dir) else {}
    fresh, dups, seen = [], [], set()
    for text, src in entries:
        if text in seen:
            continue
        seen.add(text)
        if text in existing:
            dups.append((text, where(existing[text], root)))
        else:
            fresh.append((text, src))
    if not fresh:
        return 0, dups

    os.makedirs(tl_dir, exist_ok=True)
    content = read_text(path) if os.path.isfile(path) else ""
    bk = Backup(root)
    fixed = detab(content)
    mode = "a"
    parts = []
    if not content.strip():
        mode = "w"
        fixed = ""
        parts.append("translate %s strings:\n\n" % lang)
    else:
        if fixed != content:  # tabs found: normalise the whole file once
            bk.save(path)
            mode = "w"
            parts.append(fixed if fixed.endswith("\n") else fixed + "\n")
            content = parts[0]
        elif not content.endswith("\n"):
            parts.append("\n\n")
        elif not content.endswith("\n\n"):
            parts.append("\n")
        if not _ends_in_strings_block(content, lang):
            parts.append("\ntranslate %s strings:\n\n" % lang)
    for text, src in fresh:
        q = quote_rpy(text)
        note = re.sub(r"[\r\n\t]+", " ", src).strip()
        parts.append("    # %s\n    old %s\n    new %s\n\n" % (note, q, q if prefill else '""'))
    chunk = "".join(parts)
    strict_rpy(chunk, filename)  # raises if anything non-4-space slipped in
    with open(path, mode, encoding="utf-8", newline="\n") as f:
        f.write(chunk)
    return len(fresh), dups


# =====================================================================
# 6. RPA reading + rpyc string harvesting
# =====================================================================
class RpaError(Exception):
    pass


class RpaNotFound(Exception):
    pass


class _IndexUnpickler(pickle.Unpickler):
    """An RPA index holds only dict/list/tuple/int/str/bytes. Refuse every global except the
    harmless helpers a Python-3 pickle uses for bytes, so a hostile archive cannot run code."""

    def find_class(self, module, name):
        if (module, name) == ("_codecs", "encode"):
            return lambda s, enc: s.encode(enc)
        if module in ("builtins", "__builtin__") and name in ("bytes", "bytearray"):
            return bytes
        raise pickle.UnpicklingError("blocked global %s.%s" % (module, name))


def find_rpa_files(game_dir):
    out = []
    skip = os.path.abspath(os.path.join(game_dir, "tl"))
    for d, dirs, files in os.walk(game_dir):
        dirs.sort()
        if os.path.abspath(d) == skip:
            dirs[:] = []
            continue
        for n in sorted(files):
            if n.lower().endswith(".rpa"):
                out.append(os.path.join(d, n))
    out.sort(key=lambda p: (os.path.basename(p).lower() != "scripts.rpa", p.lower()))
    return out


def read_rpa_index(f):
    """-> {name: (offset, length, prefix_bytes)} for RPA-3.0 / RPA-2.0."""
    f.seek(0)
    header = f.readline(512)
    parts = header.split()
    if not parts or parts[0] not in (b"RPA-3.0", b"RPA-2.0"):
        raise RpaError("unsupported or invalid header %r (only RPA-2.0 / RPA-3.0)" % header[:24])
    try:
        offset = int(parts[1], 16)
        key = int(parts[2], 16) if parts[0] == b"RPA-3.0" else 0
    except (IndexError, ValueError):
        raise RpaError("damaged header %r" % header[:40])
    try:
        f.seek(offset)
        raw = zlib.decompress(f.read())
    except (zlib.error, OSError) as e:
        raise RpaError("index is not valid zlib data: %s" % e)
    try:
        index = _IndexUnpickler(io.BytesIO(raw), encoding="latin1").load()
    except Exception as e:
        raise RpaError("index could not be read safely: %s" % e)
    if not isinstance(index, dict):
        raise RpaError("index has an unexpected type")
    out = {}
    for name, entries in index.items():
        if isinstance(name, bytes):
            name = name.decode("utf-8", "replace")
        if not entries:
            continue
        e = entries[0]
        prefix = e[2] if len(e) > 2 else b""
        if isinstance(prefix, str):
            prefix = prefix.encode("latin1")
        out[name] = (e[0] ^ key, e[1] ^ key, prefix)
    return out


def rpyc_payload(data):
    """Raw .rpyc bytes -> decompressed pickle bytes (RPC2 slot 1, or RPC1 whole file)."""
    if data[:10] == b"RENPY RPC2":
        pos, limit, payload = 10, min(len(data), 1024), None
        while pos + 12 <= limit:
            slot, start, length = struct.unpack("<III", data[pos:pos + 12])
            if slot == 0:
                break
            if slot == 1:
                payload = data[start:start + length]
                break
            pos += 12
        if payload is None:
            return None
    else:
        payload = data
    return zlib.decompress(payload)


# ---- fast pickle string walker -------------------------------------------------------
# Walks the opcode stream of a pickle (protocols 0-5) with plain integer arithmetic and
# collects every string. Nothing is executed or imported. ~5-10x faster than
# pickletools.genops; on any unknown opcode we fall back to genops for that file.
_FIXED = [-1] * 256   # opcode -> fixed argument size in bytes
_LENW = [0] * 256     # opcode -> width of the length prefix (0 = none)
_COLLECT = [False] * 256
_LINE = [0] * 256     # opcode -> number of newline-terminated arguments


def _init_pickle_tables():
    for ch in "()]}012NRabdelostu":                        # no argument (MARK, POP, DUP, ...)
        _FIXED[ord(ch)] = 0
    for op in (0x85, 0x86, 0x87, 0x88, 0x89, 0x81, 0x8f, 0x90, 0x91, 0x92, 0x93, 0x94, 0x51, 0x6f):
        _FIXED[op] = 0                                      # TUPLE1-3, NEWTRUE/FALSE, NEWOBJ, EMPTY_SET, ...
    for op, n in ((0x71, 1), (0x68, 1), (0x4b, 1), (0x80, 1), (0x82, 1), (0x4d, 2), (0x83, 2),
                  (0x4a, 4), (0x6a, 4), (0x72, 4), (0x84, 4), (0x47, 8), (0x95, 8)):
        _FIXED[op] = n                                      # BINPUT, BINGET, BININT*, PROTO, EXT*, BINFLOAT, FRAME
    for op, w, coll in ((0x55, 1, True), (0x54, 4, True), (0x58, 4, True), (0x8c, 1, True), (0x8d, 8, True),
                        (0x43, 1, False), (0x42, 4, False), (0x8e, 8, False), (0x96, 8, False),
                        (0x8a, 1, False), (0x8b, 4, False)):
        _LENW[op] = w
        _COLLECT[op] = coll
    for ch in "FILgpP":
        _LINE[ord(ch)] = 1
    for ch in "ci":
        _LINE[ord(ch)] = 2


_init_pickle_tables()


def _walk_pickle(b):
    out = []
    i, n = 0, len(b)
    fixed, lenw, collect, line = _FIXED, _LENW, _COLLECT, _LINE
    find = b.find
    frm = int.from_bytes
    while i < n:
        op = b[i]
        i += 1
        sz = fixed[op]
        if sz >= 0:
            i += sz
            continue
        w = lenw[op]
        if w:
            ln = b[i] if w == 1 else frm(b[i:i + w], "little")
            i += w
            if collect[op]:
                out.append(b[i:i + ln])
            i += ln
            continue
        k = line[op]
        if k:
            for _ in range(k):
                j = find(b"\n", i)
                if j < 0:
                    raise ValueError("unterminated line argument")
                i = j + 1
            continue
        if op == 0x2e:  # STOP
            break
        raise ValueError("opcode 0x%02x not handled" % op)  # S, V, ... -> genops fallback
    return out


def _walk_genops(b):
    out = []
    try:
        for op, arg, _pos in pickletools.genops(b):
            if not isinstance(arg, str):
                continue
            if op.name in ("BINUNICODE", "SHORT_BINUNICODE", "UNICODE", "BINUNICODE8"):
                out.append(arg.encode("utf-8", "surrogatepass"))
            elif op.name in ("SHORT_BINSTRING", "BINSTRING", "STRING"):
                out.append(arg.encode("latin1", "replace"))
    except Exception:
        pass  # truncated/odd stream: keep what was read
    return out


def pickle_strings(blob):
    """All strings stored in a pickle, decoded as UTF-8 (undecodable ones are binary junk -> dropped)."""
    try:
        raw = _walk_pickle(blob)
    except Exception:
        raw = _walk_genops(blob)
    out = []
    for r in raw:
        if len(r) >= MIN_LEN:
            try:
                out.append(r.decode("utf-8"))
            except UnicodeDecodeError:
                pass
    return out


# ---- text filtering ------------------------------------------------------------------
_TAGS_RE = re.compile(r"\{[^}]*\}|\[[^\]]*\]")
_CODE_RE = re.compile(
    r"\brenpy\.|\bpersistent\.|\bstore\.|\bconfig\.|\bgui\.|\bpreferences\."
    r"|^\s*(?:import|from)\s+\w+|^\s*def\s+\w+\s*\(|\blambda\b|\bself\."
    r"|==|!=|<=|>=|\+=|-=|\s=\s|\w\(|\)\s*:|^\s*\$|\n[ \t]{2,}\S")
_EXT_RE = re.compile(
    r"\.(?:png|jpe?g|webp|gif|ogg|mp3|wav|opus|webm|mkv|mp4|ttf|otf|rpyc?|rpymc?|rpa|txt|json|pyc?|ini|cfg|xml|html)$",
    re.I)
_SINGLE_RE = re.compile(r"[A-Z][A-Za-z'\-]{3,}[.!?\u2026,:;]*|[A-Za-z][A-Za-z'\-]{2,}[.!?\u2026]+")
_PUNCT_RE = re.compile(r"[.!?,\u2026:;]")
_SNAKE_RE = re.compile(r"\b\w+_\w+\b")
INTERNAL = {
    "None", "True", "False", "Default", "Fixed", "Frame", "Window", "Null", "Solid", "Text", "Transform",
    "Return", "Pass", "Class", "Style", "Screen", "Label", "Jump", "Call", "Init", "Python", "Image",
    "Motion", "Dissolve", "Fade", "Move", "Pause", "Linear", "Ease", "Hover", "Idle", "Insensitive",
    "Selected", "Button", "Textbutton", "Vbox", "Hbox", "Grid", "Viewport", "Vpgrid", "Imagebutton",
}
_PYLIT = r'''[uUbB]?(?:"(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*')'''
_PYLIT_RE = re.compile(_PYLIT)
_PYSEQ_RE = re.compile(_PYLIT + r"(?:\s*,\s*" + _PYLIT + r")*", re.S)
_HEXRUN_RE = re.compile(r"(?:\\x[0-9a-fA-F]{2})+")
_UNI_RE = re.compile(r"\\u([0-9a-fA-F]{4})")


def is_text(s):
    """Does s look like text a player can read (dialogue, menu, UI) rather than a keyword,
    file name, identifier or code?"""
    if len(s) > 1500 or len(s.strip()) < MIN_LEN:
        return False
    for ch in s:
        if ord(ch) < 32 and ch not in "\n\r\t":
            return False  # binary junk
    st = s.strip()
    if st in INTERNAL or st.startswith("_") or st.startswith("renpy"):
        return False
    if _CODE_RE.search(s):
        return False
    core = _TAGS_RE.sub("", st).strip()
    letters = sum(ch.isalpha() for ch in core)
    if len(core) < MIN_LEN or letters < 3 or letters < 0.5 * len(core):
        return False
    if _SNAKE_RE.search(core):
        return False
    if not re.search(r"\s", core):
        if "/" in core or "\\" in core or "://" in core or _EXT_RE.search(core):
            return False
        return bool(_SINGLE_RE.fullmatch(core))
    words = core.split()
    if core == core.lower() and not _PUNCT_RE.search(core) and len(words) <= 3:
        return False  # "bg room", "mia happy": image / ATL names
    return True


def _unescape_repr(c):
    """Escapes as Python's repr() wrote them: \\xNN runs (Python-2 UTF-8 bytes), \\uNNNN, \\n, \\'."""
    def hexrun(m):
        raw = bytes.fromhex(m.group(0).replace("\\x", ""))
        try:
            return raw.decode("utf-8")
        except UnicodeError:
            return raw.decode("latin1")
    c = _HEXRUN_RE.sub(hexrun, c)
    c = _UNI_RE.sub(lambda m: chr(int(m.group(1), 16)), c)
    return unescape_source(c)


def unwrap_pyrepr(s, depth=0):
    """If the WHOLE string is a Python tuple/list/paren repr made only of string literals -
    "('Text')", "('Text',)", '["Text"]', '("Text")', "(u'Text')", nested "(('Text',),)" -
    return the plain texts inside; otherwise None. Dialogue that merely contains brackets
    ("(Laughs) Hello", "Hi (there)") never matches, because after removing the outer pair
    nothing but quoted literals may remain."""
    t = s.strip()
    if depth > 4 or len(t) < 4 or (t[0] + t[-1]) not in ("()", "[]"):
        return None
    inner = t[1:-1].strip()
    if inner.endswith(","):
        inner = inner[:-1].rstrip()
    if _PYSEQ_RE.fullmatch(inner):
        return [_unescape_repr(m.group(0).lstrip("uUbB")[1:-1]) for m in _PYLIT_RE.finditer(inner)]
    return unwrap_pyrepr(inner, depth + 1)


def candidate_texts(strings):
    """Yield readable texts out of raw pickle strings."""
    for s in strings:
        wrapped = unwrap_pyrepr(s)
        if wrapped is not None:
            for c in wrapped:
                if is_text(c):
                    yield c
            continue
        st = s.strip()
        m = _LIT_RE.fullmatch(st) if st[:1] in "\"'uUrR" else None
        if m:  # whole string is one literal: screen text expression like "Quit game"
            c = unescape_source(m.group(1)[1:-1])
            if is_text(c):
                yield c
        elif is_text(s):  # dialogue, menu captions, other plain strings
            yield s
        elif '"' in s or "'" in s:  # python source: $ lines, _("..."), screen code
            for lm in _LIT_RE.finditer(s):
                c = unescape_source(lm.group(1)[1:-1])
                if is_text(c):
                    yield c


def extract_rpa_strings(game_dir, lang, root, prefill, log, progress):
    """Read every .rpa under game/ (read-only, one member at a time, nothing written to disk
    except the final .rpy), collect texts, drop the ones already in tl/<lang>/, append the rest
    to tl/<lang>/zz_extracted_dialogues.rpy. Returns a result dict."""
    rpas = find_rpa_files(game_dir)
    if not rpas:
        raise RpaNotFound(game_dir)
    log("\nExtract: %d archive(s): %s\n" % (len(rpas), ", ".join(os.path.basename(p) for p in rpas)))
    found, scanned, unreadable, bad_members = {}, 0, 0, 0
    for rpa in rpas:
        arch = os.path.basename(rpa)
        progress(0, 1, "Reading index of %s ..." % arch)
        try:
            f = open(rpa, "rb")
        except OSError as e:
            log("  skip %s: %s\n" % (arch, e))
            continue
        with f:
            try:
                index = read_rpa_index(f)
            except RpaError as e:
                log("  skip %s: %s\n" % (arch, e))
                continue
            size = os.fstat(f.fileno()).st_size
            names = sorted(n for n in index if n.lower().endswith((".rpyc", ".rpymc")))
            log("  %s: %d compiled script file(s)\n" % (arch, len(names)))
            for i, name in enumerate(names, 1):
                off, length, prefix = index[name]
                if off < 0 or length < len(prefix) or off + length - len(prefix) > size:
                    bad_members += 1
                    continue
                f.seek(off)
                data = prefix + f.read(length - len(prefix))
                try:
                    blob = rpyc_payload(data)
                except (zlib.error, struct.error):
                    blob = None
                del data
                if not blob:
                    unreadable += 1
                else:
                    src = "%s:%s" % (arch, name)
                    for t in candidate_texts(pickle_strings(blob)):
                        if t not in found:
                            found[t] = src
                    scanned += 1
                if i % 20 == 0 or i == len(names):
                    progress(i, len(names), "%s: %d/%d scripts, %d unique texts" % (arch, i, len(names), len(found)))
    if not scanned:
        raise RpaError("The archives were read, but no readable .rpyc files were found.")

    tl_dir = os.path.join(game_dir, "tl", lang)
    progress(1, 1, "Comparing with game/tl/%s ..." % lang)
    already = scan_comment_texts(tl_dir) if os.path.isdir(tl_dir) else set()
    entries = [(t, src) for t, src in found.items() if t not in already]
    soft_skipped = len(found) - len(entries)
    written, dups = add_old_entries(root, lang, EXTRACTED_NAME, entries, prefill)
    return {"scripts": scanned, "unreadable": unreadable, "bad_members": bad_members, "unique": len(found),
            "in_dialogue_blocks": soft_skipped, "duplicates": len(dups), "written": written,
            "target": os.path.join(tl_dir, EXTRACTED_NAME)}


# =====================================================================
# 7. search in .rpy files
# =====================================================================
def scan_file(path, phrase_l, any_quoted, quick):
    """phrase_l: lower-case phrase. Returns [(line_no, literal)]."""
    text = read_text(path)
    if quick and phrase_l not in text.lower():
        return []
    hits = []
    for i, line in enumerate(text.split("\n"), 1):
        if quick and phrase_l not in line.lower():
            continue
        if any_quoted:
            if line.lstrip().startswith("#"):
                continue
            for m in _LIT_RE.finditer(line):
                if phrase_l in unescape_source(m.group(1)[1:-1]).lower():
                    hits.append((i, m.group(1)))
        else:
            m = _OLD_LINE_RE.match(line)
            if m and phrase_l in unescape_source(m.group(1)[1:-1]).lower():
                hits.append((i, m.group(1)))
    return hits


def search_rpy(root, base, phrase, any_quoted, progress):
    phrase_l = phrase.lower()
    quick = not any(c in phrase_l for c in "\"'\\")  # escapes in the file could hide the raw phrase
    files = _rpy_files(base)
    results, capped = [], False
    for i, path in enumerate(files, 1):
        rel = os.path.relpath(path, root).replace("\\", "/")
        for line_no, lit in scan_file(path, phrase_l, any_quoted, quick):
            results.append({"path": path, "rel": rel, "line": line_no, "lit": lit})
            if len(results) >= MAX_RESULTS:
                capped = True
                break
        if capped:
            break
        if i % 25 == 0 or i == len(files):
            progress(i, len(files), "Scanning %d/%d files, %d result(s)" % (i, len(files), len(results)))
    return results, len(files), capped


# =====================================================================
# 8. GUI
# =====================================================================
class App:
    def __init__(self, root):
        self.root = root
        root.title(APP_NAME)
        root.minsize(940, 760)
        self.q = queue.Queue()
        self.busy = False
        self.buttons = []
        self.results = {}
        self.search_ctx = {}

        V = tk.StringVar
        self.game = V()
        self.lang = V()
        self.display = V()
        self.direction = V(value="rtl")
        self.font = V()
        self.replace = V()
        self.right_align = tk.BooleanVar(value=False)
        self.regenerate = tk.BooleanVar(value=False)
        self.hotkey = V(value="shift_K_l")
        self.button = V(value="none")
        self.prefill = tk.BooleanVar(value=False)
        self.phrase = V()
        self.global_scope = tk.BooleanVar(value=False)
        self.any_quoted = tk.BooleanVar(value=False)
        self.status = V(value="Select the game folder to begin.")
        self.rpa_status = V(value="Idle.")

        self._set_icon()
        self._build()
        root.after(100, self._poll)

    # ---- plumbing -------------------------------------------------------------------
    def _set_icon(self):
        try:
            self._icon = tk.PhotoImage(file=resource_path("icon.png"))
            self.root.iconphoto(True, self._icon)
        except Exception:
            pass  # missing icon is not an error

    def ui(self, fn):
        """Run fn on the GUI thread (safe to call from workers)."""
        self.q.put(fn)

    def _poll(self):
        try:
            while True:
                self.q.get_nowait()()
        except queue.Empty:
            pass
        except Exception:
            self._log("\n[internal error]\n%s\n" % traceback.format_exc())
        self.root.after(80, self._poll)

    def _log(self, text):
        self.logbox.configure(state="normal")
        self.logbox.insert("end", text)
        self.logbox.see("end")
        self.logbox.configure(state="disabled")

    def log(self, text):          # thread-safe
        self.ui(lambda: self._log(text))

    def progress(self, done, total, text):   # thread-safe
        def _do():
            self.bar.configure(maximum=max(total, 1), value=done)
            self.rpa_status.set(text)
            self.status.set(text)
        self.ui(_do)

    def set_busy(self, busy):
        self.busy = busy
        for b in self.buttons:
            b.configure(state="disabled" if busy else "normal")

    def run_task(self, title, fn, on_ok=None):
        if self.busy:
            return
        self.set_busy(True)

        def work():
            try:
                res = fn()
                if on_ok:
                    self.ui(lambda: on_ok(res))
            except RpaNotFound:
                self.ui(lambda: self._fail(title, "No .rpa archive (such as scripts.rpa) was found in the game "
                                                  "folder.\nCheck that you selected the game root and that this is "
                                                  "the packed PC release."))
            except Exception as e:
                tb = traceback.format_exc()
                self.ui(lambda: self._fail(title, str(e) or type(e).__name__, tb))
            finally:
                self.ui(lambda: self.set_busy(False))
        threading.Thread(target=work, daemon=True).start()

    def _fail(self, title, msg, tb=None):
        self._log("\n[%s failed] %s\n" % (title, msg))
        if tb:
            self._log(tb + "\n")
        messagebox.showerror(title, msg)

    # ---- inputs ---------------------------------------------------------------------
    def need_root(self):
        try:
            return resolve_root(self.game.get())
        except ValueError as e:
            messagebox.showerror("Game folder", str(e))
            return None

    def lang_code(self):
        try:
            return make_code(self.lang.get())
        except ValueError as e:
            messagebox.showerror("Language", str(e))
            return None

    def params(self):
        return {"name": self.lang.get(), "display": self.display.get(), "direction": self.direction.get(),
                "font": self.font.get(), "replace": [x.strip() for x in self.replace.get().split(",") if x.strip()],
                "right_align": self.right_align.get(), "regenerate": self.regenerate.get()}

    # ---- auto detection -------------------------------------------------------------
    def browse_game(self):
        d = filedialog.askdirectory(title="Select the game root folder (contains game/ and renpy/)")
        if d:
            self.game.set(d)
            self.on_game_changed()

    def on_game_changed(self, _event=None):
        try:
            root, game = resolve_root(self.game.get())
        except ValueError:
            self.lang_box["values"] = []
            self.status.set("Not a game folder yet.")
            return
        langs = list_languages(game)
        self.lang_box["values"] = langs
        if not langs:
            self.status.set("Game folder OK. No language found in game/tl/ - create one in tab 1.")
            return
        self._log("Detected languages in game/tl: %s\n" % ", ".join(langs))
        cur = self.lang.get().strip().lower()
        if cur not in [l.lower() for l in langs]:
            self.lang.set("persian" if "persian" in langs else langs[0])
        self.load_settings()

    def load_settings(self, _event=None):
        try:
            root, game = resolve_root(self.game.get())
            code = make_code(self.lang.get())
        except ValueError:
            return
        info = detect_language(game, code)
        if not info:
            self.status.set("'%s' has no zz_lang_%s.rpy yet - fill the fields and install." % (code, code))
            return
        self.display.set(info["display"])
        self.direction.set("rtl" if info["rtl"] else "ltr")
        if info["font_abs"]:
            self.font.set(info["font_abs"])
            if not os.path.isfile(info["font_abs"]):
                self._log("WARNING: font %s (from %s) is missing on disk.\n" % (info["font_rel"], info["source"]))
        self.replace.set(", ".join(info["replace"]))
        self.right_align.set(info["right_align"])
        self._log("Loaded '%s' from %s: display '%s', %s, font %s%s\n" % (
            code, info["source"], info["display"], "RTL" if info["rtl"] else "LTR", info["font_rel"] or "?",
            (", %d entries in %s" % (info["custom_entries"], CUSTOM_NAME)) if info["custom_entries"] else ""))
        self.status.set("Settings for '%s' loaded." % code)

    # ---- actions: tab 1 / 2 -----------------------------------------------------------
    def act_language(self, dry):
        r = self.need_root()
        if not r:
            return
        if not dry and not messagebox.askyesno(
                "Install language", "Write the language files into the game folder?\n"
                                    "Overwritten files are backed up to _lang_backup."):
            return
        p = self.params()
        self.run_task("Language", lambda: install_language(r[0], p, dry, self.log),
                      on_ok=lambda _res: self.on_game_changed() if not dry else None)

    def act_menu(self, mode):
        r = self.need_root()
        if not r:
            return
        if mode == "remove" and not messagebox.askyesno("Remove menu", "Remove zz_lang_menu.rpy (backed up first)?"):
            return
        hk, bt = self.hotkey.get().strip() or "shift_K_l", self.button.get()
        if mode == "remove":
            self.run_task("Menu", lambda: remove_menu(r[0], False, self.log))
        else:
            self.run_task("Menu", lambda: install_menu(r[0], hk, bt, mode == "dry", self.log))

    # ---- actions: tab 3 ---------------------------------------------------------------
    def act_extract(self):
        r = self.need_root()
        code = self.lang_code() if r else None
        if not r or not code:
            return
        root, game = r
        if not find_rpa_files(game):
            messagebox.showerror("RPA archive not found",
                                 "scripts.rpa (or any .rpa file) was not found in:\n%s\n\nCheck that you selected "
                                 "the game root folder and that this is the packed PC release." % game)
            return
        prefill = self.prefill.get()
        self.rpa_status.set("Starting ...")
        self._log("\n$ Extract RPA -> game/tl/%s/%s\n" % (code, EXTRACTED_NAME))

        def done(res):
            self.rpa_status.set("Done: %d new string(s) written." % res["written"])
            self.log("\nResult:\n"
                     "  compiled scripts read          : %d (unreadable %d, bad members %d)\n"
                     "  unique texts found             : %d\n"
                     "  already in dialogue blocks     : %d\n"
                     "  skipped, old \"...\" existed     : %d\n"
                     "  new strings written            : %d\n" % (
                         res["scripts"], res["unreadable"], res["bad_members"], res["unique"],
                         res["in_dialogue_blocks"], res["duplicates"], res["written"]))
            if res["written"]:
                self.log("  file: %s\n  Fill in the empty new \"\" lines (UTF-8), then restart the game.\n" % res["target"])
            messagebox.showinfo("Extract RPA", "Scripts read: %d\nNew strings written: %d\nSkipped duplicates: %d" % (
                res["scripts"], res["written"], res["duplicates"]))
        self.run_task("Extract RPA",
                      lambda: extract_rpa_strings(game, code, root, prefill, self.log, self.progress), on_ok=done)

    # ---- actions: tab 4 ---------------------------------------------------------------
    def act_search(self):
        r = self.need_root()
        code = self.lang_code() if r else None
        if not r or not code:
            return
        root, game = r
        phrase = self.phrase.get().strip()
        if len(phrase) < 2:
            messagebox.showinfo("Search", "Type at least 2 characters.")
            return
        base = game if self.global_scope.get() else os.path.join(game, "tl", code)
        if not os.path.isdir(base):
            messagebox.showerror("Search", "Folder not found:\n%s" % base)
            return
        any_q = self.any_quoted.get()
        self.tree.delete(*self.tree.get_children())
        self.results.clear()

        def show(res):
            results, nfiles, capped = res
            for item in results:
                iid = self.tree.insert("", "end", values=(item["rel"], item["line"], item["lit"]))
                self.results[iid] = item
            msg = "%d result(s) in %d file(s)." % (len(results), nfiles)
            if capped:
                msg += " Stopped at %d - refine the phrase." % MAX_RESULTS
            elif not results:
                msg += " Nothing found. Try the global scope or 'any quoted string'."
            self.status.set(msg)
        self.run_task("Search", lambda: search_rpy(root, base, phrase, any_q, self.progress), on_ok=show)

    def selected(self):
        return [self.results[i] for i in self.tree.selection() if i in self.results]

    def open_path(self, path):
        try:
            open_in_editor(path)
        except Exception as e:
            messagebox.showerror("Open file", "Could not open:\n%s\n\n%s" % (path, e))

    def on_double(self, event):
        iid = self.tree.identify_row(event.y)
        if iid in self.results:
            self.open_path(self.results[iid]["path"])

    def act_add_custom(self):
        sel = self.selected()
        if not sel:
            messagebox.showinfo("Add to Custom Translation", "Select one or more results first.")
            return
        r = self.need_root()
        code = self.lang_code() if r else None
        if not r or not code:
            return
        entries = [(unescape_source(s["lit"][1:-1]), "%s:%d" % (s["rel"], s["line"])) for s in sel]
        try:
            written, dups = add_old_entries(r[0], code, CUSTOM_NAME, entries, False)
        except (OSError, ValueError) as e:
            messagebox.showerror("Add to Custom Translation", "Could not write:\n%s" % e)
            return
        lines = []
        if written:
            lines.append("Added %d string(s) to game/tl/%s/%s" % (written, code, CUSTOM_NAME))
        if dups:
            lines.append("\nSkipped %d - an old \"...\" entry already exists (a duplicate would crash Ren'Py):" % len(dups))
            for text, loc in dups[:8]:
                short = text if len(text) <= 50 else text[:47] + "..."
                lines.append("  %s  ->  %s" % (short, loc))
            if len(dups) > 8:
                lines.append("  ... and %d more" % (len(dups) - 8))
        self.status.set("Added %d, skipped %d duplicate(s)." % (written, len(dups)))
        self._log("Custom: added %d, skipped %d duplicate(s).\n" % (written, len(dups)))
        messagebox.showinfo("Add to Custom Translation", "\n".join(lines).strip() or "Nothing to add.")

    def open_custom(self):
        r = self.need_root()
        code = self.lang_code() if r else None
        if not r or not code:
            return
        path = os.path.join(r[1], "tl", code, CUSTOM_NAME)
        if not os.path.isfile(path):
            messagebox.showinfo("Custom file", "%s does not exist yet. Add a result first." % CUSTOM_NAME)
            return
        self.open_path(path)

    # ---- layout ---------------------------------------------------------------------
    def _btn(self, parent, text, cmd, side="left"):
        b = ttk.Button(parent, text=text, command=cmd)
        b.pack(side=side, padx=6, pady=4)
        self.buttons.append(b)
        return b

    def _build(self):
        pad = {"padx": 6, "pady": 3}
        top = ttk.Frame(self.root, padding=8)
        top.pack(fill="both", expand=True)

        g = ttk.LabelFrame(top, text="Game")
        g.pack(fill="x", **pad)
        g.columnconfigure(1, weight=1)
        ttk.Label(g, text="Game root folder").grid(row=0, column=0, sticky="w", **pad)
        e = ttk.Entry(g, textvariable=self.game)
        e.grid(row=0, column=1, sticky="ew", **pad)
        e.bind("<Return>", self.on_game_changed)
        e.bind("<FocusOut>", self.on_game_changed)
        ttk.Button(g, text="Browse...", command=self.browse_game).grid(row=0, column=2, **pad)
        ttk.Label(g, text="Language").grid(row=1, column=0, sticky="w", **pad)
        self.lang_box = ttk.Combobox(g, textvariable=self.lang)
        self.lang_box.grid(row=1, column=1, sticky="ew", **pad)
        self.lang_box.bind("<<ComboboxSelected>>", self.load_settings)
        self.lang_box.bind("<Return>", self.load_settings)
        ttk.Label(g, text="existing folders are listed; type a new name (e.g. persian) to create one",
                  foreground="gray").grid(row=1, column=2, sticky="w", **pad)

        nb = ttk.Notebook(top)
        nb.pack(fill="x", **pad)

        # tab 1
        t1 = ttk.Frame(nb, padding=6)
        nb.add(t1, text="1. Language")
        t1.columnconfigure(1, weight=1)

        def row(r, label, var, browse=None):
            ttk.Label(t1, text=label).grid(row=r, column=0, sticky="w", **pad)
            ttk.Entry(t1, textvariable=var).grid(row=r, column=1, sticky="ew", **pad)
            if browse:
                ttk.Button(t1, text="Browse...", command=browse).grid(row=r, column=2, **pad)

        def pick_font():
            p = filedialog.askopenfilename(filetypes=[("Fonts", "*.ttf *.otf *.ttc"), ("All files", "*.*")])
            if p:
                self.font.set(p)
        row(0, "Display name (shown in menu)", self.display)
        ttk.Label(t1, text="Text direction").grid(row=1, column=0, sticky="w", **pad)
        d = ttk.Frame(t1)
        d.grid(row=1, column=1, sticky="w")
        ttk.Radiobutton(d, text="RTL", variable=self.direction, value="rtl").pack(side="left", **pad)
        ttk.Radiobutton(d, text="LTR", variable=self.direction, value="ltr").pack(side="left", **pad)
        row(2, "Font file", self.font, pick_font)
        row(3, "Replace fonts (comma list, optional)", self.replace)
        o = ttk.Frame(t1)
        o.grid(row=4, column=1, sticky="w")
        ttk.Checkbutton(o, text="Right-align text (RTL only)", variable=self.right_align).pack(side="left", **pad)
        ttk.Checkbutton(o, text="Regenerate skeleton on next start", variable=self.regenerate).pack(side="left", **pad)
        b1 = ttk.Frame(t1)
        b1.grid(row=5, column=0, columnspan=3, sticky="e")
        self._btn(b1, "Preview", lambda: self.act_language(True))
        self._btn(b1, "Install / update language", lambda: self.act_language(False))

        # tab 2
        t2 = ttk.Frame(nb, padding=6)
        nb.add(t2, text="2. In-game menu")
        ttk.Label(t2, text="Hotkey").pack(side="left", **pad)
        ttk.Entry(t2, textvariable=self.hotkey, width=14).pack(side="left", **pad)
        ttk.Label(t2, text="Corner button").pack(side="left", **pad)
        ttk.Combobox(t2, textvariable=self.button, values=["none", "tl", "tr", "bl", "br"], width=6,
                     state="readonly").pack(side="left", **pad)
        self._btn(t2, "Remove menu", lambda: self.act_menu("remove"), "right")
        self._btn(t2, "Install menu", lambda: self.act_menu("install"), "right")
        self._btn(t2, "Preview", lambda: self.act_menu("dry"), "right")

        # tab 3
        t3 = ttk.Frame(nb, padding=6)
        nb.add(t3, text="3. Extract RPA")
        ttk.Label(t3, justify="left", text=(
            "Reads scripts.rpa (read-only), pulls the texts out of the compiled .rpyc files and writes\n"
            "the ones missing from your translation files to tl/<language>/%s." % EXTRACTED_NAME)
        ).pack(anchor="w", **pad)
        r3 = ttk.Frame(t3)
        r3.pack(fill="x")
        self._btn(r3, "Extract RPA & Generate Strings", self.act_extract)
        ttk.Checkbutton(r3, text="Prefill new with the English text", variable=self.prefill).pack(side="left", **pad)
        self.bar = ttk.Progressbar(t3, mode="determinate")
        self.bar.pack(fill="x", **pad)
        ttk.Label(t3, textvariable=self.rpa_status, anchor="w").pack(fill="x", **pad)

        # tab 4
        t4 = ttk.Frame(nb, padding=6)
        nb.add(t4, text="4. Custom text")
        s = ttk.Frame(t4)
        s.pack(fill="x")
        en = ttk.Entry(s, textvariable=self.phrase)
        en.pack(side="left", fill="x", expand=True, **pad)
        en.bind("<Return>", lambda e: self.act_search())
        self._btn(s, "Search", self.act_search)
        o4 = ttk.Frame(t4)
        o4.pack(fill="x")
        ttk.Checkbutton(o4, text="Search entire game/ folder (not only tl/<lang>)",
                        variable=self.global_scope).pack(side="left", **pad)
        ttk.Checkbutton(o4, text="Also match any quoted string (text \"...\", _(\"...\"))",
                        variable=self.any_quoted).pack(side="left", **pad)
        tf = ttk.Frame(t4)
        tf.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(tf, columns=("file", "line", "text"), show="headings",
                                 selectmode="extended", height=6)
        for c, w, txt in (("file", 280, "File (relative to game root)"), ("line", 60, "Line"), ("text", 400, "Found string")):
            self.tree.heading(c, text=txt)
            self.tree.column(c, width=w, stretch=(c == "text"), anchor="e" if c == "line" else "w")
        vs = ttk.Scrollbar(tf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        vs.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Double-1>", self.on_double)
        b4 = ttk.Frame(t4)
        b4.pack(fill="x")
        self._btn(b4, "Add to Custom Translation", self.act_add_custom)
        self._btn(b4, "Open " + CUSTOM_NAME, self.open_custom)

        # log + status
        fl = ttk.LabelFrame(top, text="Log")
        fl.pack(fill="both", expand=True, **pad)
        self.logbox = tk.Text(fl, height=10, wrap="word", state="disabled")
        sb = ttk.Scrollbar(fl, command=self.logbox.yview)
        self.logbox.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.logbox.pack(fill="both", expand=True)
        ttk.Label(top, textvariable=self.status, anchor="w", relief="sunken").pack(fill="x", **pad)
        self._log("%s %s\nPick the game root folder; existing languages are detected automatically.\n"
                  "Order: tab 1 -> start the game once -> tabs 3/4 -> translate -> tab 2.\n" % (APP_NAME, APP_VERSION))


def main():
    if tk is None:
        sys.stderr.write("tkinter is not available in this Python installation.\n")
        sys.exit(1)
    if sys.platform.startswith("win"):
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    app = App(root)

    def crash(exc, val, tb):
        text = "".join(traceback.format_exception(exc, val, tb))
        try:
            app._log("\n[error]\n%s\n" % text)
            messagebox.showerror(APP_NAME, "Unexpected error:\n%s" % val)
        except Exception:
            pass
    root.report_callback_exception = crash
    root.mainloop()


if __name__ == "__main__":
    main()
