#!/usr/bin/env python3
"""
Simple reusable preflight UI for experiments.

Provides a minimal Tkinter dialog with an OK/Cancel confirmation and
a console fallback for headless environments.
Place this module under `experiments/common/scripts` so notebooks and
scripts can import it directly (or via sys.path insertion).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import List, Optional, Sequence

import json


DEFAULT_CHECKLIST: List[str] = [
    "Trial metadata reviewed and correct",
    "Sufficient disk space for bag recording",
    "Workspace clear of obstacles near robot paths",
]


def load_checklist(checklist_path: Optional[Path] = None) -> List[str]:
    """Load checklist entries from JSON config.

    Accepted formats:
    - ["item 1", "item 2", ...]
    - {"checklist": ["item 1", "item 2", ...]}
    - {"checklist": [{"text": "item", "check": "mocap_stream", "args": {...}}, ...]}

    The third form is the modular pre-flight framework (#87), where an entry may
    reference the check script that verifies it. Only the human-readable text is
    shown here; running the checks is run_preflight.py's job.
    """
    if checklist_path is None:
        return list(DEFAULT_CHECKLIST)

    path = Path(checklist_path)
    if not path.exists() or not path.is_file():
        print(f"[preflight_ui] Checklist config not found: {path}", file=sys.stderr)
        return list(DEFAULT_CHECKLIST)

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[preflight_ui] Failed to parse checklist config {path}: {exc}", file=sys.stderr)
        return list(DEFAULT_CHECKLIST)

    if isinstance(data, list):
        raw_entries = data
    elif isinstance(data, dict):
        raw_entries = data.get("checklist", [])
    else:
        raw_entries = []

    entries = []
    for item in raw_entries:
        # Object entries carry the display string in "text"; str() on the dict
        # would put a Python repr in front of the operator.
        text = str(item.get("text", "") if isinstance(item, dict) else item).strip()
        if text:
            entries.append(text)

    return entries or list(DEFAULT_CHECKLIST)


def _console_confirm(checklist: Sequence[str]) -> bool:
    print("\nPre-Operations Safety Checklist:\n")
    for item in checklist:
        print(f" - {item}")
    print("")
    try:
        resp = input("Type 'ok' to confirm and continue, anything else to cancel: ").strip().lower()
    except EOFError:
        return False
    return resp == "ok"


def show_preflight_dialog(
    checklist: Optional[Sequence[str]] = None,
    checklist_path: Optional[Path] = None,
    title: str = "Gate checklist",
) -> bool:
    """Show a simple checklist dialog. Return True if user confirms.

    Uses Tkinter when a display is available; otherwise falls back to console.
    """
    resolved_checklist = list(checklist) if checklist is not None else load_checklist(checklist_path)

    try:
        import tkinter as tk
        from tkinter import ttk
    except Exception:
        print("[preflight_ui] tkinter import failed in this environment; falling back to console.", file=sys.stderr)
        return _console_confirm(resolved_checklist)

    # If no DISPLAY, fall back to console
    if not os.environ.get("DISPLAY"):
        print("[preflight_ui] DISPLAY environment variable not set; falling back to console.", file=sys.stderr)
        return _console_confirm(resolved_checklist)

    try:
        root = tk.Tk()
        # Attempt to ensure the window appears above others (helpful when running from notebooks)
        try:
            root.lift()
            root.attributes("-topmost", True)
            root.update()
            # Remove topmost after a short delay so it doesn't stay permanently above all windows
            root.after(150, lambda: root.attributes("-topmost", False))
            root.focus_force()
        except Exception as _front_exc:
            print(f"[preflight_ui] Warning: could not force dialog to front: {_front_exc}", file=sys.stderr)
        root.title(title)
        frm = ttk.Frame(root, padding=12)
        frm.pack(fill="both", expand=True)

        lbl = ttk.Label(
            frm,
            text=(
                "Please confirm the gate checklist before starting real-hardware "
                "operations:"),
            wraplength=520,
        )
        lbl.pack(anchor="w", pady=(0, 8))

        for item in resolved_checklist:
            ttk.Label(frm, text=f"• {item}", wraplength=520).pack(anchor="w", padx=8, pady=2)

        result = {"ok": False}

        def on_ok() -> None:
            result["ok"] = True
            root.destroy()

        def on_cancel() -> None:
            result["ok"] = False
            root.destroy()

        btns = ttk.Frame(frm)
        btns.pack(fill="x", pady=10)
        ttk.Button(btns, text="OK", command=on_ok).pack(side="left", padx=(0, 8))
        ttk.Button(btns, text="Cancel", command=on_cancel).pack(side="left")

        root.mainloop()
        return bool(result["ok"])
    except Exception as exc:
        # Any UI error -> console fallback, but provide a diagnostic message
        print(f"[preflight_ui] UI error, falling back to console: {exc}", file=sys.stderr)
        return _console_confirm(resolved_checklist)


__all__ = ["DEFAULT_CHECKLIST", "load_checklist", "show_preflight_dialog"]
