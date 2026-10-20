"""Passive capture of Plaso playback traffic with mitmproxy.

Only responses the Plaso client itself requests are saved; nothing extra is
sent to any server. TLS is intercepted only for Plaso domains, every other
connection passes through untouched.

Usage:
    python plaso_capture.py --port 8899 [--root DIR]
    python plaso_capture.py --install-cert      # trust the mitmproxy CA (current user)

The GUI sets / restores the Windows system proxy itself; run with
--system-proxy to let this process do it when used standalone.
"""

import argparse
import asyncio
import datetime
import json
import os
import re
import subprocess
import sys
import traceback
import urllib.parse

from plaso_pipeline import LOCK_NAME, MAP_NAME, acquire_lock, release_lock, save_map

MEDIA_HOSTS = ("filecdn.plaso.com", "filecdn-t.plaso.com")
ALLOW_HOSTS = (r"plaso\.(com|cn|com\.cn)",)
MEDIA_EXTS = (".ts", ".m4s", ".m4a", ".aac", ".mp4", ".key")
SKIP_TAGS = ("#EXT-X-MEDIA-SEQUENCE", "#EXT-X-ENDLIST", "#EXT-X-PLAYLIST-TYPE")
SEQUENCE_RE = re.compile(r"^\d+\.[A-Za-z0-9]+$")
URI_ATTR_RE = re.compile(r'URI="([^"]+)"')
TITLE_HINT = "getfileinfo"
DEFAULT_ROOT = os.path.join(os.path.expanduser("~"), "Downloads", "plaso")
CA_CERT = os.path.join(os.path.expanduser("~"), ".mitmproxy", "mitmproxy-ca-cert.cer")
LOG_NAME = "capture.log"


def _hosts():
    extra = os.environ.get("PLASO_CAPTURE_HOSTS", "")
    return MEDIA_HOSTS + tuple(h.strip() for h in extra.split(",") if h.strip())


class Capture:
    def __init__(self, root, pad=5):
        self.root = os.path.abspath(os.path.expanduser(root))
        self.pad = pad
        self.saved = 0
        os.makedirs(self.root, exist_ok=True)

    # ---------------------------------------------------------------- logging
    def log(self, message):
        line = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} capture: {message}"
        print(line, flush=True)
        try:
            with open(os.path.join(self.root, LOG_NAME), "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    # ---------------------------------------------------------------- helpers
    def mirror(self, url_path):
        return os.path.join(self.root, url_path.lstrip("/").replace("/", os.sep))

    @staticmethod
    def url_key(uri, base_url):
        path = urllib.parse.urlparse(urllib.parse.urljoin(base_url, uri)).path
        return urllib.parse.unquote(path)

    @staticmethod
    def parse_playlist(text):
        header, blocks, cur, started = [], [], [], False
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith("#"):
                if line.startswith(SKIP_TAGS):
                    continue
                (cur if started else header).append(line)
            else:
                started = True
                cur.append(line)
                blocks.append(cur)
                cur = []
        return header, blocks

    @staticmethod
    def find_map(directory):
        cur = directory
        for _ in range(4):
            candidate = os.path.join(cur, MAP_NAME)
            if os.path.exists(candidate):
                return candidate
            parent = os.path.dirname(cur)
            if parent == cur:
                break
            cur = parent
        return None

    @staticmethod
    def load_map(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        if not isinstance(data.get("segments"), dict):
            data["segments"] = {}
        if not isinstance(data.get("next"), int) or data["next"] < 1:
            data["next"] = 1
        return data

    # ---------------------------------------------------------------- handlers
    def on_playlist(self, url, text):
        path = urllib.parse.unquote(urllib.parse.urlparse(url).path)
        mirror = self.mirror(path)
        os.makedirs(os.path.dirname(mirror), exist_ok=True)
        with open(mirror, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)

        header, blocks = self.parse_playlist(text)
        if any(line.startswith("#EXT-X-STREAM-INF") for line in header):
            self.log(f"master playlist (ignored): {url}")
            return

        playlist_dir = os.path.dirname(mirror)
        local_file = os.path.join(playlist_dir, "local.m3u8")
        map_file = os.path.join(playlist_dir, MAP_NAME)
        base_path = os.path.dirname(path)
        lock = os.path.join(playlist_dir, LOCK_NAME)
        locked = acquire_lock(lock)
        try:
            data = self.load_map(map_file)
            segments = data["segments"]
            assets = data.get("assets") if isinstance(data.get("assets"), dict) else {}
            data["assets"] = assets

            def localize(line):
                def sub(m):
                    uri = m.group(1)
                    if uri.startswith(("data:", "skd:")):
                        return m.group(0)
                    key = self.url_key(uri, url)
                    name = assets.get(key)
                    if name is None:
                        kind = "init" if line.startswith("#EXT-X-MAP") else "key"
                        ext = os.path.splitext(key)[1] or (".mp4" if kind == "init" else ".key")
                        name = f"{kind}_{len(assets) + 1:03d}{ext}"
                        assets[key] = name
                    return f'URI="{name}"'
                return URI_ATTR_RE.sub(sub, line)

            header = [localize(l) if l.startswith(("#EXT-X-KEY", "#EXT-X-MAP")) else l
                      for l in header]
            for i, block in enumerate(blocks):
                tags = [localize(l) if l.startswith(("#EXT-X-KEY", "#EXT-X-MAP")) else l
                        for l in block[:-1]]
                key = self.url_key(block[-1], url)
                name = segments.get(key)
                if name is None:
                    ext = os.path.splitext(key)[1] or ".ts"
                    name = f"{data['next']:0{self.pad}d}{ext}"
                    segments[key] = name
                    data["next"] += 1
                    old = self.mirror(key)
                    new = os.path.join(playlist_dir, name)
                    try:
                        if os.path.exists(old) and not os.path.exists(new):
                            os.replace(old, new)
                    except OSError:
                        pass
                blocks[i] = tags + [name]
            data.setdefault("playlist_url", url)
            data.setdefault("playlist_dir", base_path + "/")
            data["endlist"] = "#EXT-X-ENDLIST" in text
            data["segment_count"] = len(segments)
            save_map(map_file, data)

            merged, seen = [], set()
            if os.path.exists(local_file):
                with open(local_file, "r", encoding="utf-8", errors="replace") as f:
                    _, old_blocks = self.parse_playlist(f.read())
                for block in old_blocks:
                    if SEQUENCE_RE.match(block[-1]) and block[-1] not in seen:
                        seen.add(block[-1])
                        merged.append(block)
            for block in blocks:
                if block[-1] not in seen:
                    seen.add(block[-1])
                    merged.append(block)

            tmp = f"{local_file}.{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                f.write("#EXTM3U\n")
                for line in header:
                    if line != "#EXTM3U":
                        f.write(line + "\n")
                for block in merged:
                    f.write("\n".join(block) + "\n")
                if "#EXT-X-ENDLIST" in text:
                    f.write("#EXT-X-ENDLIST\n")
            os.replace(tmp, local_file)
        finally:
            if locked:
                release_lock(lock)
        self.log(f"playlist: {len(merged)} segments -> {playlist_dir}")

    def on_media(self, url, body):
        path = urllib.parse.unquote(urllib.parse.urlparse(url).path)
        mirror = self.mirror(path)
        dest = None
        map_file = self.find_map(os.path.dirname(mirror))
        if map_file:
            data = self.load_map(map_file)
            name = data["segments"].get(path) or (data.get("assets") or {}).get(path)
            if name:
                dest = os.path.join(os.path.dirname(map_file), name)
        if dest is None:
            dest = mirror
        if os.path.exists(dest) and os.path.getsize(dest) == len(body):
            return
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = f"{dest}.{os.getpid()}.part"
        with open(tmp, "wb") as f:
            f.write(body)
        os.replace(tmp, dest)
        self.saved += 1
        self.log(f"saved {os.path.basename(dest)} ({len(body)} bytes)")

    def on_title(self, text):
        try:
            data = json.loads(text)
        except ValueError:
            return
        obj = data.get("obj") or data.get("file") or {}
        if not isinstance(obj, dict):
            return
        location = (obj.get("location") or "").strip()
        name = (obj.get("name") or "").strip()
        if not location or not name:
            return
        class_id = location.rstrip("/").split("/")[-1]
        path = os.path.join(self.root, "titles.json")
        lock = path + ".lock"
        locked = acquire_lock(lock)
        try:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    mapping = json.load(f)
                if not isinstance(mapping, dict):
                    mapping = {}
            except (OSError, ValueError):
                mapping = {}
            entry = dict(mapping.get(class_id) or {}) if isinstance(mapping.get(class_id), dict) else {}
            entry.update({
                "title": name,
                "class_id": class_id,
                "location": location,
                "teacher": obj.get("createdBy") or "",
                "duration_s": obj.get("duration"),
                "source": "capture",
            })
            mapping[class_id] = entry
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(mapping, f, ensure_ascii=False, indent=1)
            os.replace(tmp, path)
        finally:
            if locked:
                release_lock(lock)
        self.log(f"title: {class_id} -> {name}")

    def handle(self, url, status, body, content_type=""):
        """Entry point shared by the mitmproxy addon and tests."""
        parsed = urllib.parse.urlparse(url)
        host = (parsed.hostname or "").lower()
        if TITLE_HINT in url and status == 200 and body:
            self.on_title(body.decode("utf-8", "replace"))
            return
        if host not in _hosts():
            return
        name = os.path.basename(urllib.parse.unquote(parsed.path)).lower()
        if status != 200 or not body:
            return
        if name.endswith(".m3u8"):
            self.on_playlist(url, body.decode("utf-8", "replace"))
        elif name.endswith(MEDIA_EXTS):
            self.on_media(url, body)


class PlasoAddon:
    def __init__(self, capture):
        self.capture = capture

    def response(self, flow):
        try:
            resp = flow.response
            if resp is None:
                return
            self.capture.handle(
                flow.request.pretty_url,
                resp.status_code,
                resp.get_content(strict=False) or b"",
                resp.headers.get("content-type", ""),
            )
        except Exception:
            self.capture.log("ERROR\n" + traceback.format_exc())


# ------------------------------------------------------------------ system proxy
INET_KEY = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"


def _refresh_inet():
    try:
        import ctypes

        wininet = ctypes.windll.wininet
        wininet.InternetSetOptionW(0, 39, 0, 0)  # SETTINGS_CHANGED
        wininet.InternetSetOptionW(0, 37, 0, 0)  # REFRESH
    except Exception:
        pass


def get_system_proxy():
    if os.name != "nt":
        return None
    import winreg

    out = {}
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, INET_KEY) as key:
        for name in ("ProxyEnable", "ProxyServer", "ProxyOverride"):
            try:
                out[name] = winreg.QueryValueEx(key, name)[0]
            except OSError:
                out[name] = None
    return out


def set_system_proxy(values):
    if os.name != "nt":
        return
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, INET_KEY, 0, winreg.KEY_SET_VALUE) as key:
        for name, value in values.items():
            if value is None:
                try:
                    winreg.DeleteValue(key, name)
                except OSError:
                    pass
            elif name == "ProxyEnable":
                winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, int(value))
            else:
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, str(value))
    _refresh_inet()


def enable_proxy(port):
    set_system_proxy({
        "ProxyEnable": 1,
        "ProxyServer": f"127.0.0.1:{port}",
        "ProxyOverride": "<local>",
    })


def install_cert():
    if not os.path.exists(CA_CERT):
        print(f"CA not generated yet: {CA_CERT} (start capture once first)", flush=True)
        return 1
    if os.name != "nt":
        print(f"install manually: {CA_CERT}", flush=True)
        return 1
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    res = subprocess.run(["certutil", "-user", "-addstore", "Root", CA_CERT],
                         capture_output=True, text=True, errors="replace", creationflags=flags)
    print(res.stdout.strip() or res.stderr.strip(), flush=True)
    print("cert install: " + ("ok" if res.returncode == 0 else f"failed ({res.returncode})"),
          flush=True)
    return res.returncode


# ------------------------------------------------------------------ runner
async def _run(capture, host, port):
    from mitmproxy.options import Options
    from mitmproxy.tools.dump import DumpMaster

    opts = Options(listen_host=host, listen_port=port)
    master = DumpMaster(opts, with_termlog=False, with_dumper=False)
    opts.update(allow_hosts=list(ALLOW_HOSTS) + [re.escape(h) for h in _hosts()])
    master.addons.add(PlasoAddon(capture))
    capture.log(f"listening on {host}:{port}, saving to {capture.root}")
    capture.log(f"CA certificate: {CA_CERT}")
    await master.run()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="plaso_capture")
    parser.add_argument("--root", default=os.environ.get("PLASO_DIR", DEFAULT_ROOT))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8899)
    parser.add_argument("--pad", type=int, default=int(os.environ.get("PLASO_PAD", "5")))
    parser.add_argument("--system-proxy", action="store_true",
                        help="set the Windows proxy while running and restore it on exit")
    parser.add_argument("--install-cert", action="store_true")
    args = parser.parse_args(argv)

    if args.install_cert:
        return install_cert()

    capture = Capture(args.root, args.pad)
    saved = None
    if args.system_proxy:
        saved = get_system_proxy()
        enable_proxy(args.port)
        capture.log("system proxy enabled")
    try:
        asyncio.run(_run(capture, args.host, args.port))
    except KeyboardInterrupt:
        pass
    finally:
        if saved is not None:
            set_system_proxy(saved)
            capture.log("system proxy restored")
        capture.log(f"stopped, {capture.saved} files saved")
    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        import signal

        signal.signal(signal.SIGBREAK, signal.default_int_handler)
    sys.exit(main())
