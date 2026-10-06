"""Scrollable guidance for the managed subtitle workflow."""
import tkinter as tk
from tkinter import ttk

from .tooltips import attach_tooltip


HELP_TEXT = """1. Select the game folder
Select a discovered installation or use Browse to choose The Witcher 3 folder. Rescan refreshes available resources, preview freshness, and installed-file conflicts.

2. Choose languages and a merge mode
The primary language appears first and selects the game resources to replace. Choose a different secondary language with matching resource directories.

Dialogue-only is the default: it merges matching string IDs with no key hash in either language. Keyed entries remain unchanged. Full-text also merges matching keyed text, including menus and descriptions. Text separation follows the application's punctuation rule.

Include cutscene subtitles is on by default. Generation scans game bundle indexes and hashes bundle data, which can read several GB and take a while. The preview reports estimated scan work and output size; generated files stay in application state and installed copies go under Mods/modW3DualSubtitleManager/content, so storage can be about twice the output estimate while both are kept, plus temporary working space. Clear the checkbox before generating to skip cutscene outputs. Subtitle line breaks retain the game's native NUL separator.

The built-in codec needs no external executable. Browse beside the codec field selects an optional external converter; Use built-in returns to the default. Semantic compatibility is checked during preview generation.

3. Generate and inspect a preview
Generate preview checks compatibility and creates output in application state. Review target paths and SHA-256 hashes in the preview, including cutscene targets under Mods/modW3DualSubtitleManager/content. The summary shows changed, skipped, and unchanged sidecars and videos, cue match count and ratio, estimated work and output, actual output bytes, and source freshness. Use Text unmatched CSV for interactive w3strings identities, or Cutscene unmatched CSV for unmatched cues and skipped cutscene resources. Phase progress reports actual bytes during bundle scans and streamed video work. A stale preview must be regenerated before installation.

4. Review and apply the desired action
Install applies a fresh preview, creates validated original-file backups for interactive text, and installs cutscene overrides in the dedicated managed Mods folder. Modify install updates the managed cutscene files and interactive resources while preserving the original backups; targets removed from the new preview are restored or removed from the manager's Mods folder. Uninstall removes the manager's cutscene overrides and restores the validated original interactive files. Review the exact target paths and backup information in each confirmation before proceeding.

Backups and conflicts
Original backups are retained in the per-user application state directory shown by the app. Keep them available for Modify and Uninstall. The app checks managed target hashes and original backups and stops when it finds conflicts; resolve the listed paths and rescan before continuing. Close the game before changing its resources, and wait for an operation to finish before closing this manager.
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
