import os
import socket
import sys
import threading
import urllib.request
import webbrowser

import uvicorn

import app as app_module

if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

try:
    from tray import make_icon
    TRAY_AVAILABLE = True
except Exception:
    TRAY_AVAILABLE = False


def find_running_instance():
    for port in range(8000, 8021):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=0.4) as r:
                if b"decision-journal" in r.read():
                    return port
        except Exception:
            continue
    return None


def free_port():
    for port in range(8000, 8021):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(port: int):
    app_module.tpl.env.globals["can_exit"] = True
    server = uvicorn.Server(
        uvicorn.Config(app_module.app, host="127.0.0.1", port=port, log_level="warning")
    )
    app_module.app.state.server = server
    return server


def run_with_tray(port: int):
    server = start_server(port)
    thread = threading.Thread(target=server.run, daemon=True, name="uvicorn")
    thread.start()
    threading.Timer(1.5, webbrowser.open, args=(f"http://127.0.0.1:{port}",)).start()
    icon = make_icon(port, server)
    icon.run()
    server.should_exit = True
    thread.join(timeout=5)


def run_headless(port: int):
    server = start_server(port)
    threading.Timer(1.5, webbrowser.open, args=(f"http://127.0.0.1:{port}",)).start()
    server.run()


def main():
    running = find_running_instance()
    if running:
        webbrowser.open(f"http://127.0.0.1:{running}")
        return
    port = free_port()
    if TRAY_AVAILABLE:
        run_with_tray(port)
    else:
        run_headless(port)


if __name__ == "__main__":
    main()
