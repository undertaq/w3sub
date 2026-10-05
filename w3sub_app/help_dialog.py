"""Scrollable guidance for the managed subtitle workflow."""
import tkinter as tk
from tkinter import ttk

from .tooltips import attach_tooltip


HELP_TEXT = """1. Select the game folder
Select a discovered installation or use Browse to choose The Witcher 3 folder. Rescan refreshes available resources, preview freshness, and installed-file conflicts.

2. Choose languages and a merge mode
The primary language appears first and selects the game resources to replace. Choose a different secondary language with matching resource directories.

Dialogue-only is the default: it merges matching string IDs with no key hash in either language. Keyed entries remain unchanged. Full-text also merges matching keyed text, including menus and descriptions. Text separation follows the application's punctuation rule.

The built-in codec needs no external executable. Browse beside the codec field selects an optional external converter; Use built-in returns to the default. Semantic compatibility is checked during preview generation.

3. Generate and inspect a preview
Generate preview checks compatibility and creates output in application state. Review target paths and SHA-256 hashes in the preview, and use Open unmatched list to inspect entries that could not be matched. Phase progress shows completed work out of the current phase's total. A stale preview must be regenerated before installation.

4. Review and apply the desired action
Install applies a fresh preview and creates validated original-file backups before replacing game resources. Modify install applies a new preview to an active install while preserving the original backups; targets removed from the new preview are restored. Uninstall restores the validated originals. Review the exact target paths and backup information in each confirmation before proceeding.

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
