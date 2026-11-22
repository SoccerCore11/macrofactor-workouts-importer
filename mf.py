"""ADB driver for MacroFactor Workouts (built and tested on a Google Pixel Tablet, 2560x1600 landscape).

CLI (exploration):
  python mf.py dump [regex]        compact list of labelled nodes
  python mf.py tap X Y | tapt REGEX [index]
  python mf.py text "..." | key KEYCODE | swipe X1 Y1 X2 Y2 [ms]
  python mf.py clock "YYYY-MM-DD HH:MM[:SS]" | clock auto
"""
import os
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime

# adb ships with scrcpy and the Android SDK platform-tools; set MF_ADB if it is not on PATH
ADB = os.environ.get("MF_ADB") or shutil.which("adb") or "adb"
PKG = "com.sbs.train"


class UIError(RuntimeError):
    pass


def adb(*args, timeout=30):
    for attempt in range(5):
        r = subprocess.run([ADB, *args], capture_output=True, timeout=timeout)
        err = r.stderr.decode(errors="replace")
        # the command never reached the device when the local daemon socket fails, so rerunning is safe
        if r.returncode == 0 or "cannot connect to daemon" not in err:
            break
        time.sleep(2 * (attempt + 1))
    if r.returncode != 0:
        raise UIError(f"adb {' '.join(args)} failed: {r.stderr.decode(errors='replace')}")
    return r.stdout.decode("utf-8", errors="replace")


def sh(cmd, timeout=30):
    return adb("shell", cmd, timeout=timeout)


class Node:
    __slots__ = ("label", "text", "desc", "x1", "y1", "x2", "y2", "clickable",
                 "cls", "checked", "focused", "scrollable", "idx")

    def __init__(self, e, idx):
        self.text = e.get("text", "")
        self.desc = e.get("content-desc", "")
        self.label = self.text or self.desc
        b = re.findall(r"\d+", e.get("bounds", "[0,0][0,0]"))
        self.x1, self.y1, self.x2, self.y2 = map(int, b)
        self.clickable = e.get("clickable") == "true"
        self.scrollable = e.get("scrollable") == "true"
        self.checked = e.get("checked") == "true"
        self.focused = e.get("focused") == "true"
        self.cls = e.get("class", "").split(".")[-1]
        self.idx = idx

    @property
    def c(self):
        return ((self.x1 + self.x2) // 2, (self.y1 + self.y2) // 2)

    def __repr__(self):
        lab = self.label.replace("\n", " | ")
        if len(lab) > 110:
            lab = lab[:107] + "..."
        flags = ("C" if self.clickable else "") + ("S" if self.scrollable else "") + \
                ("F" if self.focused else "") + ("X" if self.checked else "")
        return f"{self.idx:3d} {self.cls[:10]:10s} {flags:3s} {self.c[0]:4d},{self.c[1]:4d} {lab}"


_U2 = None
APP_PKG = "com.sbs.train"
PAUSE_SCALE = float(os.environ.get("MF_PAUSE_SCALE", "0.5"))  # 0.5 validated across hundreds of workouts
_TIMER_RE = re.compile(r'"\d+:\d\d:\d\d"')


def _u2_xml():
    """Fast hierarchy dump via the uiautomator2 on-device agent (user-approved; ~0.15 s vs ~2.2 s).
    Waits until two consecutive dumps agree, since the old slow dump implicitly let transitions finish."""
    global _U2
    if _U2 is None:
        if os.path.isabs(ADB):
            os.environ.setdefault("ADBUTILS_ADB_PATH", ADB)
        import uiautomator2
        _U2 = uiautomator2.connect()
    time.sleep(0.3 * PAUSE_SCALE)
    prev = None
    for _ in range(8):
        xml = _U2.dump_hierarchy()
        key = _TIMER_RE.sub("", xml)
        if key == prev:
            return xml
        prev = key
        time.sleep(0.3 * PAUSE_SCALE)
    return xml


def drag(x, y1, y2, ms=500, pause=0.3):
    """Momentum-free scroll: the pointer rests at the end point before lifting, so the list moves exactly y1-y2."""
    _u2_xml()
    # u2 spends `duration` on EACH segment: one move, then one stationary hold that zeroes the fling velocity
    _U2.swipe_points([(x, y1), (x, y2), (x, y2)], duration=ms / 2000)
    time.sleep(pause * PAUSE_SCALE)


def set_text_edit(text):
    """Replace the focused/only EditText's content in one call via the u2 agent; True if it now reads `text`."""
    if os.environ.get("MF_U2", "1") != "1":
        return False
    try:
        _u2_xml()  # ensures the agent connection
        el = _U2(className="android.widget.EditText")
        el.set_text(text)
        time.sleep(0.2 * PAUSE_SCALE)
        return el.get_text() == text
    except Exception:
        return False


def dump(retries=4):
    last = None
    use_u2 = os.environ.get("MF_U2", "1") == "1"
    for i in range(retries):
        try:
            if use_u2:
                xml = _u2_xml()
            else:
                out = adb("exec-out", "uiautomator dump /dev/tty", timeout=40)
                xml = out[out.find("<?xml"):out.rfind(">") + 1]
            root = ET.fromstring(xml)
            nodes = []
            for e in root.iter("node"):
                if use_u2 and e.get("package") != APP_PKG:
                    continue  # u2 also returns system UI windows (status/nav bars)
                nodes.append(Node(e, len(nodes)))
            return nodes
        except Exception as ex:  # idle-state failures are transient
            last = ex
            if use_u2 and i >= 1:
                global _U2
                _U2 = None  # reconnect (restarts the agent if it died)
            time.sleep(0.7)
    raise UIError(f"dump failed: {last}")


def labelled(nodes):
    return [n for n in nodes if n.label or n.clickable or n.scrollable or n.cls == "EditText"]


def find(nodes, pattern, flags=re.S):
    rx = re.compile(pattern, flags)
    return [n for n in nodes if n.label and rx.search(n.label)]


def tap(x, y, pause=0.6):
    sh(f"input tap {int(x)} {int(y)}")
    time.sleep(pause * PAUSE_SCALE)


def tap_node(n, pause=0.6):
    tap(*n.c, pause=pause)


def swipe(x1, y1, x2, y2, ms=400, pause=0.6):
    sh(f"input swipe {int(x1)} {int(y1)} {int(x2)} {int(y2)} {int(ms)}")
    time.sleep(pause * PAUSE_SCALE)


def key(code, pause=0.3):
    sh(f"input keyevent {code}")
    time.sleep(pause * PAUSE_SCALE)


def _quote(s):
    return "'" + s.replace("'", "'\\''") + "'"


def type_text(s, pause=0.4):
    """Type ASCII text. Spaces become %s per `input text` rules."""
    if any(ord(ch) > 126 or ch in "\n\t" for ch in s):
        raise UIError(f"type_text cannot send non-ASCII/newline: {s!r}")
    for chunk in re.split(r"( )", s):
        if not chunk:
            continue
        if chunk == " ":
            key("KEYCODE_SPACE", pause=0.05)
        else:
            sh("input text " + _quote(chunk))
    time.sleep(pause * PAUSE_SCALE)


def clear_field(n_chars=60):
    key("KEYCODE_MOVE_END", pause=0.05)
    sh("input keyevent " + " ".join(["KEYCODE_DEL"] * n_chars))
    time.sleep(0.2)


def wait_for(pattern, timeout=8.0, interval=0.5):
    end = time.time() + timeout
    while True:
        nodes = dump()
        hits = find(nodes, pattern)
        if hits:
            return nodes, hits
        if time.time() > end:
            raise UIError(f"timeout waiting for /{pattern}/")
        time.sleep(interval)


def tap_label(pattern, i=0, timeout=8.0, pause=0.6):
    nodes, hits = wait_for(pattern, timeout)
    tap_node(hits[i], pause=pause)
    return hits[i]


def set_clock(dt: datetime):
    sh("settings put global auto_time 0")
    ms = int(dt.timestamp() * 1000)  # assumes the computer and the device use the same time zone
    sh(f"cmd alarm set-time {ms}")
    time.sleep(0.5)


def clock_auto():
    sh("settings put global auto_time 1")


def device_now():
    return sh("date '+%Y-%m-%d %H:%M:%S'").strip()


def foreground_ok():
    out = sh("dumpsys window | grep mCurrentFocus")
    return PKG in out


def screenshot(path=None, width=1024, crop=None):
    import io
    import os
    import tempfile
    from PIL import Image
    r = subprocess.run([ADB, "exec-out", "screencap", "-p"], capture_output=True, timeout=30)
    im = Image.open(io.BytesIO(r.stdout)).convert("RGB")
    if crop:
        im = im.crop(crop)
    im = im.resize((width, int(im.height * width / im.width)))
    path = path or os.path.join(tempfile.gettempdir(), f"mf_shot_{int(time.time() * 1000)}.jpg")
    im.save(path, quality=70)
    return path


def show(pattern=None):
    nodes = dump()
    sel = labelled(nodes)
    if pattern:
        sel = [n for n in sel if n.label and re.search(pattern, n.label, re.S | re.I)]
    for n in sel:
        print(n)


if __name__ == "__main__":
    a = sys.argv[1:]
    cmd = a[0] if a else "dump"
    if cmd == "dump":
        show(a[1] if len(a) > 1 else None)
    elif cmd == "tap":
        tap(a[1], a[2])
    elif cmd == "tapt":
        tap_label(a[1], int(a[2]) if len(a) > 2 else 0)
    elif cmd == "text":
        type_text(a[1])
    elif cmd == "key":
        key(a[1])
    elif cmd == "swipe":
        swipe(*a[1:5], *(a[5:6] or [400]))
    elif cmd == "clock":
        if a[1] == "auto":
            clock_auto()
        else:
            set_clock(datetime.fromisoformat(a[1]))
        print(device_now())
    elif cmd == "shot":
        print(screenshot(a[1] if len(a) > 1 else None))
    elif cmd == "now":
        print(device_now())
    else:
        print(__doc__)
