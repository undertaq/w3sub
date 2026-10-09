"""Scrollable guidance for the managed subtitle workflow."""
import tkinter as tk
from tkinter import ttk

from .tooltips import attach_tooltip


HELP_TEXT = """1. Select the game folder
Select a discovered installation or use Browse to choose The Witcher 3 folder. Rescan refreshes available resources, preview freshness, and installed-file conflicts.

2. Choose languages and a merge mode
The primary language appears first and selects the game resources used as input. Choose a different secondary language with matching resource directories.

Dialogue-only is the default: it merges matching string IDs with no key hash in either language. Keyed entries remain unchanged. Full-text also merges matching keyed text, including menus and descriptions. Text separation follows the application's punctuation rule.

Include cutscene subtitles is on by default. Generation scans game bundle indexes and hashes bundle data, which can read several GB and take a while. Changed cutscene sidecars and their complete companion movie files are packed together in Mods/modW3DualSubtitleManager/content/bundles/movies.bundle with a matching metadata.store. The tested 5.00 install has about 7.2 GiB of movie data; a selected-language package can be smaller. The preview reports the exact package size. Keep extra space free for generated files, temporary bundle creation, and the installed Mods copy. Clear the checkbox before generating to skip cutscene outputs. `.subs` cue rows stay CRLF-delimited, with carriage return (U+000D) inside each cue separating the languages onto two lines; embedded USM subtitle cues use the same line break.

The built-in codec needs no external executable. Browse beside the codec field selects an optional external converter; Use built-in returns to the default. Semantic compatibility is checked during preview generation.

3. Generate and inspect a preview
Generate preview checks compatibility and creates output in application state. Review target paths and SHA-256 hashes in the preview, including the cutscene movies.bundle and metadata.store. The summary shows changed, skipped, and unchanged sidecars and videos, cue match count and ratio, estimated work, actual package size, and source freshness. Use Text unmatched CSV for interactive w3strings identities, or Cutscene unmatched CSV for unmatched cues and skipped cutscene resources. Phase progress reports actual bytes during bundle scans and movie-bundle validation. A stale preview must be regenerated before installation.

4. Review and apply the desired action
Install places interactive string files and the movie bundle plus metadata under Mods/modW3DualSubtitleManager/content. The base game's content and DLC files stay untouched. Modify install updates those manager-mod files to match the preview. Uninstall removes only the manager-mod files created by this app. Review the exact target paths in each confirmation before proceeding.

Conflicts
The app checks managed target hashes and stops when it finds conflicts; resolve the listed paths and rescan before continuing. Close the game before changing its resources, and wait for an operation to finish before closing this manager.
"""


def show_help(parent: tk.Misc) -> None:
    """Open a resizable help window with a scrollable four-step workflow."""
    window = tk.Toplevel(parent)
    window.title("Subtitle Manager Help")
    window.transient(parent)
    window.geometry("660x520")
    window.minsize(440, 320)
    window.columnconfigure(0, weight=1)
    window.rowconfigure(0, weight=1)
    frame = ttk.Frame(window, padding=12)
    frame.grid(row=0, column=0, sticky="nsew")
    frame.columnconfigure(0, weight=1)
    frame.rowconfigure(0, weight=1)
    text = tk.Text(frame, wrap="word", padx=8, pady=8)
    text.grid(row=0, column=0, sticky="nsew")
    scrollbar = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
    scrollbar.grid(row=0, column=1, sticky="ns")
    text.configure(yscrollcommand=scrollbar.set)
    text.insert("1.0", HELP_TEXT)
    text.configure(state="disabled")
    close = ttk.Button(frame, text="Close", command=window.destroy)
    close.grid(row=1, column=0, columnspan=2, sticky="e", pady=(8, 0))
    attach_tooltip(close, "Close this help window and return to the subtitle manager.")
    window.bind("<Escape>", lambda _event: window.destroy())
