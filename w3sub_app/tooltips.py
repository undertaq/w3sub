"""Delayed hover hints that preserve the control's existing bindings."""
import tkinter as tk
from tkinter import ttk


def attach_tooltip(widget: tk.Widget, text: str) -> None:
    """Attach a hint without changing command, state, or selection behavior."""
    pending = None
    window = None

    def hide(_event=None):
        nonlocal pending, window
        if pending is not None:
            widget.after_cancel(pending)
            pending = None
        if window is not None:
            window.destroy()
            window = None

    def show():
        nonlocal pending, window
        pending = None
        if not widget.winfo_exists() or not widget.winfo_ismapped():
            return
        window = tk.Toplevel(widget)
        window.withdraw()
        window.overrideredirect(True)
        ttk.Label(window, text=text, wraplength=360, padding=8,
                  relief="solid", borderwidth=1).pack()
        window.update_idletasks()
        x = min(widget.winfo_rootx() + 12,
                max(0, widget.winfo_screenwidth() - window.winfo_reqwidth()))
        y = min(widget.winfo_rooty() + widget.winfo_height() + 6,
                max(0, widget.winfo_screenheight() - window.winfo_reqheight()))
        window.geometry(f"+{x}+{y}")
        window.deiconify()

    def schedule(_event=None):
        nonlocal pending
        hide()
        pending = widget.after(550, show)

    widget.bind("<Enter>", schedule, add="+")
    for event in ("<Leave>", "<ButtonPress>", "<FocusOut>", "<Destroy>"):
        widget.bind(event, hide, add="+")
