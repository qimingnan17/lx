from reqable import *

import datetime
import json
import os
import re
import time
import traceback
import urllib.parse

HOSTS = ("filecdn.plaso.com", "filecdn-t.plaso.com")
DEFAULT_DIR = os.path.join(os.path.expanduser("~"), "Downloads", "plaso")
MEDIA_EXTS = (".ts", ".m4s", ".m4a", ".aac", ".mp4", ".key")
MAP_NAME = ".plaso_map.json"
LOCK_NAME = ".plaso_map.lock"
LOG_NAME = "plaso.log"
SEQUENCE_RE = re.compile(r"^\d+\.[A-Za-z0-9]+$")
URI_ATTR_RE = re.compile(r'URI="([^"]+)"')


def _env(context, name, default=None):
    return (context.env or {}).get(name) or default


def _save_dir(context):
    return os.path.expanduser(_env(context, "PLASO_DIR", DEFAULT_DIR))


def _pad(context):
    try:
        return min(9, max(1, int(_env(context, "PLASO_PAD", "5"))))
    except (TypeError, ValueError):
        return 5


def _log(context, message):
    line = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {message}"
    print(f"[plaso] {message}", flush=True)
    try:
        root = _save_dir(context)
        os.makedirs(root, exist_ok=True)
        with open(os.path.join(root, LOG_NAME), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _body_text(body):
    if body.isText:
        return body.payload
    if body.isBinary:
        return body.payload.decode("utf-8", "replace")
    return ""


def _url_key(uri, base_url):
    path = urllib.parse.urlparse(urllib.parse.urljoin(base_url, uri)).path
    return urllib.parse.unquote(path)


def _mirror_path(root, url_path):
    return os.path.join(root, url_path.lstrip("/").replace("/", os.sep))


def _localize_uri(uri, base_path):
    parsed = urllib.parse.urlparse(uri)
    path = parsed.path if (parsed.scheme or parsed.netloc) else uri.split("?", 1)[0]
    path = urllib.parse.unquote(path)
    if path.startswith(base_path + "/"):
        path = path[len(base_path) + 1:]
    return path.lstrip("/")


def _localize_header_line(line, base_path):
    return URI_ATTR_RE.sub(
        lambda m: 'URI="%s"' % _localize_uri(m.group(1), base_path), line
    )


def _parse_playlist(text):
    header = []
    blocks = []
    cur = []
    started = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            if line.startswith(("#EXT-X-MEDIA-SEQUENCE", "#EXT-X-ENDLIST", "#EXT-X-PLAYLIST-TYPE")):
                continue
            if started:
                cur.append(line)
            else:
                header.append(line)
        else:
            started = True
            cur.append(line)
            blocks.append(cur)
            cur = []
    return header, blocks


def _acquire_lock(path, context, timeout=5.0):
    deadline = time.time() + timeout
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return True
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(path) > 30:
                    os.remove(path)
                    continue
            except OSError:
                pass
            if time.time() >= deadline:
                _log(context, f"WARN lock timeout: {path}")
                return False
            time.sleep(0.02)


def _release_lock(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _load_map(path):
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


def _save_map(path, data):
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _find_map(directory):
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


def _write_local_playlist(context, local_file, text, playlist_url, root):
    header, blocks = _parse_playlist(text)
    base_path = os.path.dirname(urllib.parse.urlparse(playlist_url).path)

    if any(line.startswith("#EXT-X-STREAM-INF") for line in header):
        for block in blocks:
            folder = os.path.dirname(_localize_uri(block[-1], base_path))
            block[-1] = (folder + "/" if folder else "") + "local.m3u8"
        with open(local_file, "w", encoding="utf-8", newline="\n") as f:
            f.write("#EXTM3U\n")
            for line in header:
                if line != "#EXTM3U":
                    f.write(line + "\n")
            for block in blocks:
                for line in block:
                    f.write(line + "\n")
        _log(context, f"master playlist saved: {local_file}")
        return

    playlist_dir = os.path.dirname(local_file)
    map_file = os.path.join(playlist_dir, MAP_NAME)
    locked = _acquire_lock(os.path.join(playlist_dir, LOCK_NAME), context)
    try:
        data = _load_map(map_file)
        segments = data["segments"]
        pad = _pad(context)

        for block in blocks:
            key = _url_key(block[-1], playlist_url)
            name = segments.get(key)
            if name is None:
                ext = os.path.splitext(key)[1] or ".ts"
                name = f"{data['next']:0{pad}d}{ext}"
                segments[key] = name
                data["next"] += 1
                old = _mirror_path(root, key)
                new = os.path.join(playlist_dir, name)
                try:
                    if os.path.exists(old) and not os.path.exists(new):
                        os.replace(old, new)
                        _log(context, f"migrated {name}")
                except OSError as exc:
                    _log(context, f"WARN migrate failed: {exc}")
            block[-1] = name

        _save_map(map_file, data)

        merged = []
        seen = set()
        if os.path.exists(local_file):
            with open(local_file, "r", encoding="utf-8", errors="replace") as f:
                _, old_blocks = _parse_playlist(f.read())
            for block in old_blocks:
                uri = block[-1]
                if SEQUENCE_RE.match(uri) and uri not in seen:
                    seen.add(uri)
                    merged.append(block)

        for block in blocks:
            uri = block[-1]
            if uri not in seen:
                seen.add(uri)
                merged.append(block)

        is_live = "#EXT-X-ENDLIST" not in text
        tmp = f"{local_file}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write("#EXTM3U\n")
            for line in header:
                if line != "#EXTM3U":
                    f.write(_localize_header_line(line, base_path) + "\n")
            for block in merged:
                for line in block:
                    f.write(line + "\n")
            if not is_live:
                f.write("#EXT-X-ENDLIST\n")
        os.replace(tmp, local_file)
        _log(context, f"playlist saved: {local_file} ({len(merged)} segments)")
    finally:
        if locked:
            _release_lock(os.path.join(playlist_dir, LOCK_NAME))


def onResponse(context, response):
    try:
        return _on_response(context, response)
    except Exception:
        _log(context, "ERROR\n" + traceback.format_exc())
        return response


def _on_response(context, response):
    url = context.url or ""
    parsed = urllib.parse.urlparse(url)
    if parsed.hostname not in HOSTS:
        return response
    path = urllib.parse.unquote(parsed.path)
    name = os.path.basename(path)
    is_playlist = name.endswith(".m3u8")
    if not is_playlist and not name.endswith(MEDIA_EXTS):
        return response
    if response.code == 304:
        _log(context, f"cache hit 304: {url}")
        return response
    if response.code != 200:
        _log(context, f"skip status {response.code}: {url}")
        return response
    if response.body.isNone or response.body.isMultipart:
        _log(context, f"empty body: {url}")
        return response

    root = _save_dir(context)
    mirror = _mirror_path(root, path)
    os.makedirs(os.path.dirname(mirror), exist_ok=True)

    if is_playlist:
        text = _body_text(response.body)
        with open(mirror, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        _write_local_playlist(
            context,
            os.path.join(os.path.dirname(mirror), "local.m3u8"),
            text,
            url,
            root,
        )
        return response

    dest = None
    map_file = _find_map(os.path.dirname(mirror))
    if map_file:
        data = _load_map(map_file)
        mapped = data["segments"].get(path)
        if mapped:
            dest = os.path.join(os.path.dirname(map_file), mapped)
    if dest is None:
        dest = mirror
        _log(context, f"unmapped media, keep path: {dest}")

    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return response

    tmp = f"{dest}.{os.getpid()}.part"
    response.body.writeFile(tmp)
    os.replace(tmp, dest)
    _log(context, f"media saved: {dest}")
    return response
