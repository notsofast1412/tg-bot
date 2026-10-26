import os
import sys
import tempfile
import unittest
from unittest.mock import patch

from paintwatch.domain import parse_ref
from paintwatch.platform import SecretStore
from paintwatch.store import Store


@unittest.skipUnless(
    sys.platform == "win32" or os.getenv("DISPLAY"), "Graphical desktop required"
)
class WindowTests(unittest.TestCase):
    def setUp(self):
        from paintwatch.ui import Window

        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.window = Window(self.store, SecretStore(self.temp.name), smoke=True)
        self.window.root.withdraw()

    def tearDown(self):
        self.window.root.destroy()
        self.temp.cleanup()

    def test_product_dialog_add_filter_edit_and_tag_selection(self):
        from paintwatch.ui import ProductDialog

        dialog = ProductDialog(self.window)
        dialog.urls.insert("1.0", "https://www.ozon.ru/product/196525150/")
        with patch("paintwatch.ui.messagebox.askyesno", return_value=True):
            dialog.save()
        product = self.store.products()[0]
        pid = str(product["id"])
        self.assertEqual(self.window.tree.get_children(), (pid,))
        self.window.tree.selection_set(pid)
        self.window.show_details()
        self.assertIn("196525150", self.window.details.get())
        dialog = ProductDialog(self.window, product)
        dialog.name.set("Моя краска")
        dialog.threshold.set("600")
        dialog.save()
        self.assertEqual(self.store.product(product["id"])["threshold"], 60000)
        self.window.search.set("нет такого товара")
        self.assertEqual(self.window.tree.get_children(), ())
        self.window.search.set("краска")
        self.assertEqual(self.window.tree.get_children(), (pid,))
        self.window.tags_tree.selection_set("#arton")
        self.window.refresh()
        self.assertEqual(self.window.tags_tree.selection(), ("#arton",))
        self.window.root.update()

    def test_pause_persists_and_settings_do_not_start_monitoring(self):
        self.store.add([parse_ref("196525150", "ozon")], "Краска", "#arton")
        self.window.refresh()
        pid = str(self.store.products()[0]["id"])
        self.window.tree.selection_set(pid)
        self.window.pause()
        self.assertFalse(self.store.product(int(pid))["enabled"])
        self.window.chat.set("-100123")
        self.window.interval.set("20")
        with patch("paintwatch.ui.autostart"):
            self.window.save_settings()
        self.assertEqual(self.store.setting("interval"), "1200")
        self.assertEqual(self.store.setting("monitoring", "0"), "0")

    def test_explicit_paste_and_russian_layout_replace_selection_once(self):
        from types import SimpleNamespace
        from paintwatch.ui import ProductDialog

        dialog = ProductDialog(self.window)
        text = "https://www.ozon.ru/product/196525150/\nhttps://www.wildberries.ru/catalog/29220564/detail.aspx"
        dialog.urls.clipboard_clear()
        dialog.urls.clipboard_append(text)
        dialog.paste_button.invoke()
        self.assertEqual(dialog.urls.get("1.0", "end-1c"), text)
        dialog.clipboard.select_all()
        dialog.clipboard.platform = "win32"
        result = dialog.clipboard.on_key(
            SimpleNamespace(state=4, keycode=86, keysym="Cyrillic_em")
        )
        self.assertEqual(result, "break")
        self.assertEqual(dialog.urls.get("1.0", "end-1c"), text)
        with patch("paintwatch.ui.messagebox.askyesno", return_value=True):
            dialog.save()
        self.assertEqual(len(self.store.products()), 2)
        self.window.root.clipboard_clear()

    def test_paste_in_entry_and_empty_clipboard_leave_existing_text(self):
        from tkinter import ttk
        from paintwatch.clipboard import ClipboardActions

        entry = ttk.Entry(self.window.root)
        actions = ClipboardActions(entry)
        entry.insert(0, "старое")
        actions.select_all()
        entry.clipboard_clear()
        entry.clipboard_append("новое")
        actions.menu.invoke(0)
        self.assertEqual(entry.get(), "новое")
        entry.clipboard_clear()
        with patch("paintwatch.clipboard.messagebox.showinfo") as info:
            actions.paste()
        info.assert_called_once()
        self.assertEqual(entry.get(), "новое")
