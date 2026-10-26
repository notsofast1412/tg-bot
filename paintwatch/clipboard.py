"""Explicit paste and editing shortcuts for non-Latin Windows layouts."""

import sys
import tkinter as tk
from tkinter import messagebox


class ClipboardActions:
    def __init__(self, widget, *, platform=None):
        self.widget = widget
        self.platform = sys.platform if platform is None else platform
        self.masked = not isinstance(widget, tk.Text) and bool(widget.cget("show"))
        self.menu = tk.Menu(widget, tearoff=False)
        self.menu.add_command(label="Вставить", command=self.paste)
        if not self.masked:
            self.menu.add_command(
                label="Копировать", command=lambda: self.edit("<<Copy>>")
            )
            self.menu.add_command(
                label="Вырезать", command=lambda: self.edit("<<Cut>>")
            )
        self.menu.add_separator()
        self.menu.add_command(label="Выделить всё", command=self.select_all)
        widget.bind("<Control-KeyPress>", self.on_key, add="+")
        widget.bind("<Button-3>", self.popup, add="+")

    def editable(self):
        return str(self.widget.cget("state")) not in ("disabled", "readonly")

    def paste(self, *, show_error=True):
        if not self.editable():
            return
        try:
            text = self.widget.clipboard_get()
        except tk.TclError:
            if show_error:
                messagebox.showinfo(
                    "Вставка",
                    "В буфере Windows нет доступного текста. Скопируйте ссылку и повторите вставку.",
                    parent=self.widget.winfo_toplevel(),
                )
            return
        self.widget.focus_set()
        if isinstance(self.widget, tk.Text):
            if self.widget.tag_ranges("sel"):
                self.widget.delete("sel.first", "sel.last")
            self.widget.insert("insert", text)
            self.widget.see("insert")
        else:
            if self.widget.selection_present():
                self.widget.delete("sel.first", "sel.last")
            self.widget.insert("insert", text)

    def select_all(self):
        self.widget.focus_set()
        if isinstance(self.widget, tk.Text):
            self.widget.tag_add("sel", "1.0", "end-1c")
            self.widget.mark_set("insert", "end-1c")
        else:
            self.widget.selection_range(0, "end")
            self.widget.icursor("end")

    def edit(self, event):
        if self.masked or (event == "<<Cut>>" and not self.editable()):
            return
        self.widget.event_generate(event)

    def on_key(self, event):
        if not event.state & 0x4 or event.state & 0x8:
            return None
        action = None
        if self.platform == "win32":
            # Virtual key codes stay the same when a Windows user switches RU/EN.
            action = {86: "paste", 65: "all", 67: "copy", 88: "cut"}.get(event.keycode)
        if action is None:
            action = {"v": "paste", "a": "all", "c": "copy", "x": "cut"}.get(
                event.keysym.lower()
            )
        if action == "paste":
            self.paste(show_error=False)
        elif action == "all":
            self.select_all()
        elif action in ("copy", "cut"):
            self.edit("<<Copy>>" if action == "copy" else "<<Cut>>")
        else:
            return None
        return "break"  # Do not also run Tk's class binding and paste twice.

    def popup(self, event):
        self.widget.focus_set()
        self.menu.entryconfigure(0, state="normal" if self.editable() else "disabled")
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()
        return "break"
