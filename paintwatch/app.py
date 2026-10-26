import argparse
from pathlib import Path
import sys
import tkinter as tk
from tkinter import messagebox

from .platform import SecretStore, data_directory, instance_lock
from .store import Store


def main():
    parser = argparse.ArgumentParser(description="PaintWatch для Windows")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--minimized", action="store_true")
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="Проверка окна без сети и фонового мониторинга",
    )
    args = parser.parse_args()
    directory = args.data_dir or data_directory()
    directory.mkdir(parents=True, exist_ok=True)
    try:
        with instance_lock(directory):
            from .ui import Window

            store = Store(directory)
            window = Window(store, SecretStore(directory), smoke=args.smoke_test)
            if args.smoke_test:
                window.root.update()
                assert window.tree.get_children() == ()
                window.root.destroy()
                print("GUI smoke test passed")
                return
            window.run(args.minimized)
    except RuntimeError as error:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("PaintWatch", str(error))
        root.destroy()
        sys.exit(1)


if __name__ == "__main__":
    main()
