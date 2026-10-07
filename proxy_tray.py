"""System tray launcher for the Claude JustDoWork Recovery Proxy.

Designed to run under pythonw.exe (no console window). The proxy is spawned as a
fully detached child process, so both the tray icon and the proxy keep running
after the terminal that started them is closed.
"""
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime

# pythonw.exe attaches no console, so sys.stdout/sys.stderr are None and any
# print() -- ours or a library's -- would raise. Replace them before anything else.
for _stream in ("stdout", "stderr"):
    if getattr(sys, _stream, None) is None:
        setattr(sys, _stream, open(os.devnull, "w", encoding="utf-8"))

from pystray import Icon, Menu, MenuItem
from PIL import Image, ImageDraw

BASE = os.path.dirname(os.path.abspath(__file__))
PROXY_SCRIPT = os.path.join(BASE, "agent_proxy.py")
KEY_FILE = os.path.join(BASE, "key.txt")
PROXY_LOG = os.path.join(BASE, "debug_log.txt")   # written by agent_proxy.py
OUT_LOG = os.path.join(BASE, "proxy_out.log")     # proxy stdout/stderr (tracebacks)
TRAY_LOG = os.path.join(BASE, "tray.log")         # this launcher's own log
PID_FILE = os.path.join(BASE, "proxy.pid")
PORT = int(os.environ.get("PORT", "8181"))

# DETACHED_PROCESS: child gets no console, so closing ours cannot signal it.
# CREATE_NEW_PROCESS_GROUP: Ctrl+C/Break in our console is not forwarded to it.
DETACHED = (getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200))

state = {"proc": None, "icon": None, "stopping": False}


def log(msg):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        with open(TRAY_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def notify(msg, title="Claude JustDoWork Recovery Proxy"):
    icon = state.get("icon")
    if not icon:
        return
    try:
        icon.notify(msg, title)
    except Exception:
        pass  # notifications are not supported on every backend


def interpreter():
    """python.exe for the child: it is a console app and logs to stdout."""
    exe = sys.executable or "python"
    console_exe = exe.replace("pythonw.exe", "python.exe")
    return console_exe if os.path.exists(console_exe) else exe


def port_in_use():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def is_running():
    proc = state.get("proc")
    return proc is not None and proc.poll() is None


def read_key():
    if os.path.exists(KEY_FILE):
        try:
            key = open(KEY_FILE, encoding="utf-8").read().strip()
            if key:
                return key
        except OSError as e:
            log(f"could not read {KEY_FILE}: {e}")
    return os.environ.get("UPSTREAM_API_KEY", "").strip()


def kill_stale_proxy():
    """Terminate a proxy left behind by a previous tray instance."""
    if not os.path.exists(PID_FILE):
        return
    try:
        pid = int(open(PID_FILE, encoding="utf-8").read().strip())
    except (OSError, ValueError):
        pid = None
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
            log(f"terminated stale proxy pid {pid}")
            time.sleep(1)
        except OSError:
            pass  # already gone
    try:
        os.remove(PID_FILE)
    except OSError:
        pass


def start_proxy():
    """Spawn agent_proxy.py detached, with output going to a file (never a PIPE).

    An undrained subprocess.PIPE deadlocks the child once the pipe buffer fills,
    and the proxy logs every request, so it would fill quickly.
    """
    if is_running():
        return True

    kill_stale_proxy()

    if port_in_use():
        log(f"port {PORT} already in use - not starting a second proxy")
        notify(f"Port {PORT} is already in use. Proxy not started.")
        return False

    key = read_key()
    if not key:
        log("no API key found (key.txt / UPSTREAM_API_KEY) - proxy will exit")
        notify("No API key found. Set one in key.txt and restart the proxy.")
        return False

    env = os.environ.copy()
    env["UPSTREAM_API_KEY"] = key

    try:
        out = open(OUT_LOG, "a", encoding="utf-8", errors="replace")
        out.write(f"\n===== proxy started {datetime.now():%Y-%m-%d %H:%M:%S} =====\n")
        out.flush()
        proc = subprocess.Popen(
            [interpreter(), PROXY_SCRIPT],
            cwd=BASE,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            creationflags=DETACHED,
            close_fds=True,
        )
    except OSError as e:
        log(f"failed to spawn proxy: {e}")
        notify(f"Could not start the proxy: {e}")
        return False
    finally:
        try:
            out.close()  # the child keeps its own inherited handle
        except Exception:
            pass

    state["proc"] = proc
    try:
        with open(PID_FILE, "w", encoding="utf-8") as f:
            f.write(str(proc.pid))
    except OSError:
        pass
    log(f"proxy spawned (pid {proc.pid}) -> http://127.0.0.1:{PORT}")

    # Confirm it actually came up; a crash on startup (bad import, bad key)
    # would otherwise be silent now that there is no console.
    for _ in range(20):
        if port_in_use():
            log("proxy is listening")
            refresh_menu()
            return True
        if proc.poll() is not None:
            log(f"proxy exited immediately (code {proc.returncode}) - see {OUT_LOG}")
            notify("Proxy failed to start. Check proxy_out.log.")
            refresh_menu()
            return False
        time.sleep(0.5)

    log(f"proxy did not open port {PORT} in time - see {OUT_LOG}")
    notify("Proxy did not start listening. Check proxy_out.log.")
    refresh_menu()
    return False


def stop_proxy():
    proc = state.get("proc")
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        log(f"proxy stopped (pid {proc.pid})")
    state["proc"] = None
    try:
        os.remove(PID_FILE)
    except OSError:
        pass


def watchdog():
    """Notice an unexpected proxy exit and report it once."""
    while not state["stopping"]:
        proc = state.get("proc")
        if proc is not None and proc.poll() is not None and not state["stopping"]:
            log(f"proxy exited unexpectedly (code {proc.returncode}) - see {OUT_LOG}")
            notify("The proxy stopped unexpectedly. Check proxy_out.log.")
            state["proc"] = None
            refresh_menu()
        time.sleep(3)


def refresh_menu():
    icon = state.get("icon")
    if icon:
        try:
            icon.update_menu()
        except Exception:
            pass


def open_path(path, what):
    if not os.path.exists(path):
        notify(f"No {what} yet.")
        return
    try:
        os.startfile(path)  # default editor, instead of hardcoding notepad
    except Exception as e:
        log(f"could not open {path}: {e}")


# --- menu actions (pystray passes icon and item) ---

def on_view_logs(icon, item):
    open_path(PROXY_LOG, "request log")


def on_view_output(icon, item):
    open_path(OUT_LOG, "proxy output")


def on_view_tray_log(icon, item):
    open_path(TRAY_LOG, "tray log")


def on_restart(icon, item):
    log("restart requested")
    stop_proxy()
    time.sleep(0.5)
    start_proxy()


def on_delete_key(icon, item):
    if os.path.exists(KEY_FILE):
        try:
            os.remove(KEY_FILE)
            log("API key deleted")
            notify("API key deleted. The proxy has been stopped.")
        except OSError as e:
            log(f"could not delete key: {e}")
            return
    else:
        notify("No key.txt to delete.")
    stop_proxy()
    refresh_menu()


def on_exit(icon, item):
    log("exit requested")
    state["stopping"] = True
    stop_proxy()
    icon.stop()


def status_text(item):
    return f"Proxy: {'running' if is_running() else 'stopped'} (port {PORT})"


def build_menu():
    return Menu(
        MenuItem(status_text, None, enabled=False),
        Menu.SEPARATOR,
        MenuItem("View Request Log", on_view_logs, default=True),
        MenuItem("View Proxy Output", on_view_output),
        MenuItem("View Tray Log", on_view_tray_log),
        Menu.SEPARATOR,
        MenuItem("Restart Proxy", on_restart),
        MenuItem("Delete API Key", on_delete_key),
        Menu.SEPARATOR,
        MenuItem("Exit", on_exit),
    )


def build_image():
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((2, 2, 62, 62), fill=(0, 122, 204, 255))
    d.rectangle((18, 29, 46, 35), fill=(255, 255, 255, 255))
    d.polygon([(40, 20), (52, 32), (40, 44)], fill=(255, 255, 255, 255))
    return img


def setup(icon):
    """Runs once the tray loop is up. A custom setup must set visible itself.

    The proxy is started on another thread because pystray joins the setup
    thread on shutdown, and a slow setup would stall Exit.
    """
    icon.visible = True
    threading.Thread(target=start_proxy, daemon=True).start()


def main():
    log("tray starting")
    state["icon"] = Icon(
        "claude-justdowork-recovery-proxy",
        build_image(),
        title=f"Claude JustDoWork Recovery Proxy - http://127.0.0.1:{PORT}",
        menu=build_menu(),
    )
    threading.Thread(target=watchdog, daemon=True).start()
    state["icon"].run(setup=setup)
    log("tray stopped")


if __name__ == "__main__":
    main()
