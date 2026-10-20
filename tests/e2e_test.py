"""End-to-end smoke test: plain + AES-128 HLS, CDN pacing, mitmproxy capture.

    python tests/e2e_test.py [--exe path/to/Plaso.exe]

Generates test streams with ffmpeg, serves them on localhost, then drives
plaso_get (init / fetch / verify / merge) and plaso_capture through a real
proxy. Exits non-zero on any failure.
"""

import argparse
import functools
import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def ffmpeg_bin(name):
    for cand in (os.path.join(HERE, "ffmpeg", name + ".exe"), os.path.join(HERE, name + ".exe")):
        if os.path.isfile(cand):
            return cand
    return shutil.which(name) or name


def make_stream(dest, key=False):
    os.makedirs(dest, exist_ok=True)
    cmd = [ffmpeg_bin("ffmpeg"), "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", "testsrc=d=12:s=320x240:r=25",
           "-f", "lavfi", "-i", "sine=d=12",
           "-c:v", "libx264", "-c:a", "aac", "-hls_time", "2", "-hls_playlist_type", "vod"]
    if key:
        with open(os.path.join(dest, "enc.key"), "wb") as f:
            f.write(os.urandom(16))
        info = os.path.join(dest, "..", "keyinfo.txt")
        with open(info, "w") as f:
            f.write("enc.key\n" + os.path.join(dest, "enc.key") + "\n")
        cmd += ["-hls_key_info_file", info]
    cmd.append(os.path.join(dest, "index.m3u8"))
    subprocess.run(cmd, check=True)


def serve(root):
    port = free_port()
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=root)
    handler.log_message = lambda *a, **k: None
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port


class Runner:
    def __init__(self, exe):
        self.exe = exe

    def cmd(self, tool):
        if self.exe:
            return [self.exe, tool]
        script = {"get": "plaso_get.py", "capture": "plaso_capture.py"}[tool]
        return [sys.executable, os.path.join(HERE, script)]

    def run(self, tool, *args, timeout=180):
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        res = subprocess.run(self.cmd(tool) + list(args), capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=timeout, env=env)
        out = res.stdout + res.stderr
        print(f"$ {tool} {' '.join(args)}\n{out.strip()}\n(exit {res.returncode})\n", flush=True)
        return res.returncode, out


def check(cond, msg):
    if not cond:
        print("FAIL: " + msg, flush=True)
        sys.exit(1)
    print("ok: " + msg, flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", default="")
    args = ap.parse_args()
    r = Runner(args.exe)
    tmp = tempfile.mkdtemp(prefix="plaso_e2e_")
    srv = os.path.join(tmp, "srv")
    make_stream(os.path.join(srv, "liveclass", "plaso", "1", "abc", "a1"))
    make_stream(os.path.join(srv, "liveclass", "plaso", "1", "enc", "a1"), key=True)
    httpd, port = serve(srv)
    base = f"http://127.0.0.1:{port}"

    # 1) init / fetch (paced) / verify for plain and encrypted
    for cls in ("abc", "enc"):
        out_root = os.path.join(tmp, "out")
        code, _ = r.run("get", "init", "--root", out_root, "--no-cache", "--base", base,
                        "--playlist", f"{base}/liveclass/plaso/1/{cls}/a1/index.m3u8",
                        "--title", cls)
        check(code == 0, f"init {cls}")
        sdir = os.path.join(out_root, "liveclass", "plaso", "1", cls, "a1")
        t0 = time.time()
        code, out = r.run("get", "fetch", "--root", out_root, "--no-cache", "--base", base,
                          "--dir", sdir, "--merge", "--force", "--speed", "8")
        check(code == 0 and "complete" in out, f"fetch+merge {cls}")
        check(time.time() - t0 > 1.0, f"playback pacing slowed requests ({cls})")
        code, out = r.run("get", "verify", "--root", out_root, "--no-cache", "--dir", sdir)
        check(code == 0 and "verify: PASS" in out, f"verify {cls}")

    # 2) cdn cap
    out_root = os.path.join(tmp, "cap")
    r.run("get", "init", "--root", out_root, "--no-cache", "--base", base,
          "--playlist", f"{base}/liveclass/plaso/1/abc/a1/index.m3u8")
    sdir = os.path.join(out_root, "liveclass", "plaso", "1", "abc", "a1")
    code, out = r.run("get", "fetch", "--root", out_root, "--no-cache", "--base", base,
                      "--dir", sdir, "--cdn-max", "1", "--pace", "jitter",
                      "--delay-max", "0")
    check(code == 2 and "cdn capped" in out, "cdn-max caps fetching")

    # 3) capture through a real mitmproxy instance
    cap_root = os.path.join(tmp, "capture")
    cport = free_port()
    env = dict(os.environ, PLASO_CAPTURE_HOSTS="127.0.0.1", PYTHONIOENCODING="utf-8")
    proc = subprocess.Popen(r.cmd("capture") + ["--port", str(cport), "--root", cap_root],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace", env=env)
    try:
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", cport), timeout=1).close()
                break
            except OSError:
                time.sleep(0.5)
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": f"http://127.0.0.1:{cport}"}))
        play = f"{base}/liveclass/plaso/1/abc/a1/index.m3u8"
        text = opener.open(play, timeout=20).read().decode()
        segs = [l for l in text.splitlines() if l and not l.startswith("#")]
        for seg in segs:
            opener.open(f"{base}/liveclass/plaso/1/abc/a1/{seg}", timeout=20).read()
        time.sleep(2)
    finally:
        proc.terminate()
        try:
            out, _ = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()
        print(out, flush=True)
    sdir = os.path.join(cap_root, "liveclass", "plaso", "1", "abc", "a1")
    with open(os.path.join(sdir, ".plaso_map.json"), encoding="utf-8") as f:
        mapping = json.load(f)
    names = list(mapping["segments"].values())
    have = [n for n in names if os.path.exists(os.path.join(sdir, n))]
    check(len(names) == len(segs) and len(have) == len(names), "capture saved every segment")
    code, out = r.run("get", "fetch", "--root", cap_root, "--no-cache", "--no-cdn",
                      "--dir", sdir, "--merge", "--force")
    check(code == 0 and "merge: done" in out, "merge captured session without CDN")

    httpd.shutdown()
    print("ALL PASSED", flush=True)


if __name__ == "__main__":
    main()
