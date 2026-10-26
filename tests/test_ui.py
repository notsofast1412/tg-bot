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
