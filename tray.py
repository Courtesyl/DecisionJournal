import webbrowser

import pystray
from PIL import Image

from app import BASE_DIR

ICON_PATH = BASE_DIR / "static" / "icon.png"


def make_icon(port: int, server) -> pystray.Icon:
    image = Image.open(ICON_PATH)
    url = f"http://127.0.0.1:{port}"

    def on_open(_icon, _item):
        webbrowser.open(url)

    def on_new(_icon, _item):
        webbrowser.open(f"{url}/new")

    def on_exit(icon_, _item):
        icon_.stop()
        server.should_exit = True

    menu = pystray.Menu(
        pystray.MenuItem("Открыть", on_open, default=True),
        pystray.MenuItem("Новая запись", on_new),
        pystray.MenuItem("Выключить", on_exit),
    )
    return pystray.Icon("DecisionJournal", image, "DecisionJournal", menu)
