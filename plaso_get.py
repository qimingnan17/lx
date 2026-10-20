import argparse
import gzip
import json
import os
import posixpath
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request

from plaso_cache import CacheError, CacheReader
import plaso_titles
from plaso_pipeline import (
    DEFAULT_BASE,
    DEFAULT_CACHE,
    EXTINF_RE,
    MAP_NAME,
    LOCK_NAME,
    acquire_lock,
    CLIENT_HEADERS,
    add_transcode_args,
    fill,
    find_map,
    find_out,
    load_map,
    log,
    release_lock,
    media_paths,
    merge,
    save_map,
    tc_from_args,
    verify,
)

INFO_NAME = "info.plist"
LOCAL_M3U8 = "local.m3u8"
SOURCE_M3U8 = "source.m3u8"
DEFAULT_ROOT = os.path.join(os.path.expanduser("~"), "Downloads", "plaso")
SKIP_TAGS = ("#EXT-X-MEDIA-SEQUENCE", "#EXT-X-ENDLIST", "#EXT-X-PLAYLIST-TYPE")
SEQ_RE = re.compile(r"#EXT-X-MEDIA-SEQUENCE:(\d+)")
URI_ATTR_RE = re.compile(r'URI="([^"]+)"')
ASSET_TAGS = ("#EXT-X-KEY", "#EXT-X-MAP", "#EXT-X-SESSION-KEY")
UA = CLIENT_HEADERS


def open_cache(args, quiet=False):
    try:
        return CacheReader(args.cache_dir)
    except (OSError, CacheError) as exc:
        if not quiet:
            print(f"cache unavailable: {exc}", file=sys.stderr)
        return None


def decode_json(data):
    if not data:
        return None
    blobs = [data]
    try:
        blobs.insert(0, gzip.decompress(data))
    except (OSError, ValueError):
        pass
    for blob in blobs:
        try:
            return json.loads(blob.decode("utf-8", "replace"))
        except ValueError:
            continue
    return None


def http_get(url, timeout=30):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, headers=UA)
    with opener.open(req, timeout=timeout) as resp:
        if resp.status != 200:
            raise OSError(f"HTTP {resp.status}")
        return resp.read()


def mirror_dir(root, url_path):
    return os.path.join(root, url_path.lstrip("/").replace("/", os.sep))


def parse_blocks(text):
    header = []
    blocks = []
    cur = []
    started = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            if line.startswith(SKIP_TAGS):
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


def parse_ranges(spec):
    out = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.update(range(int(a), int(b) + 1))
        else:
            out.add(int(part))
    return out


def num_of(name):
    base = os.path.splitext(name)[0]
    return int(base) if base.isdigit() else None


def collect_sessions(args, reader):
    entries = reader.url_entries()
    titles = plaso_titles.resolve_all(args.root)
    infos = {}
    for url, entry in entries.items():
        if INFO_NAME not in url:
            continue
        base = url.split("?", 1)[0]
        data = decode_json(reader.read_body(entry))
        if not isinstance(data, dict):
            continue
        old = infos.get(base)
        if old is None or data.get("recordTime", 0) > old.get("recordTime", 0):
            infos[base] = data

    playlists = {}
    for base, data in infos.items():
        path = urllib.parse.urlparse(base).path
        cut = path.rfind("/" + INFO_NAME)
        class_path = path[:cut] if cut > 0 else posixpath.dirname(path)
        media = data.get("media") or []
        rel = media[0][3] if media and len(media[0]) > 3 and media[0][3] else ""
        if not rel:
            continue
        playlists[args.base + posixpath.join(class_path, rel)] = data

    for url in entries:
        if url.endswith(".m3u8") and url not in playlists:
            playlists[url] = None

    rows = []
    for url in sorted(playlists):
        data = playlists[url]
        playlist_dir = posixpath.dirname(urllib.parse.urlparse(url).path) + "/"
        prefix = args.base + playlist_dir
        cached_ts = sum(
            1 for u in entries if u.startswith(prefix) and u.endswith(".ts")
        )
        local = mirror_dir(args.root, playlist_dir)
        have = total = 0
        map_file = os.path.join(local, MAP_NAME)
        if os.path.exists(map_file):
            try:
                names = list(load_map(map_file)["segments"].values())
            except (OSError, ValueError):
                names = []
            total = len(names)
            have = sum(1 for n in names if os.path.exists(os.path.join(local, n)))
        media = (data or {}).get("media") or []
        dur = None
        if media and len(media[0]) > 2:
            dur = round(media[0][2] / 1000.0, 3)
        elif data and data.get("duration"):
            dur = round(data["duration"] / 1000.0, 3)
        class_id = plaso_titles.class_id_of_dir(playlist_dir)
        meta = titles.get(class_id) or {}
        rows.append(
            {
                "playlist_url": url,
                "class_id": class_id,
                "title": meta.get("title") or "",
                "teacher": meta.get("createdBy") or meta.get("teacher") or "",
                "title_source": meta.get("source") or "",
                "playlist_dir": playlist_dir,
                "duration_s": dur,
                "record_time": (data or {}).get("recordTime"),
                "resolution": (data or {}).get("d"),
                "cached_playlist": url in entries,
                "cached_ts": cached_ts,
                "local_dir": local,
                "local_have": have,
                "local_total": total,
            }
        )
    return rows


def disp_width(text):
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in text)


def pad(text, width):
    text = str(text)
    return text + " " * max(0, width - disp_width(text))


def human_dur(sec):
    if not sec:
        return "-"
    total = int(sec)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def print_table(header, rows):
    table = [header] + [[str(c) for c in r] for r in rows]
    widths = [max(disp_width(r[c]) for r in table) for c in range(len(header))]
    for r in table:
        print("  ".join(pad(cell, widths[c]) for c, cell in enumerate(r)).rstrip())


def cmd_discover(args):
    reader = open_cache(args)
    if reader is None:
        return 1
    rows = collect_sessions(args, reader)
    reader.close()
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return 0
    if not rows:
        print("no playlists found in cache")
        return 0
    table = []
    for i, row in enumerate(rows):
        total = row.get("local_total") or 0
        local = f"{row.get('local_have', 0)}/{total}" if total else "-"
        table.append(
            [
                i,
                row.get("title") or "(未命名)",
                human_dur(row.get("duration_s")),
                row.get("resolution") or "-",
                row.get("cached_ts", 0),
                local,
                row.get("class_id", ""),
            ]
        )
    print_table(["#", "课程名称", "时长", "分辨率", "缓存段", "本地", "class_id"], table)
    print("use --json for playlist urls and local dirs")
    return 0


def pick_playlist(args, reader):
    rows = collect_sessions(args, reader)
    if not rows:
        print("no playlists found in cache", file=sys.stderr)
        return None, None
    if args.index is not None:
        if 0 <= args.index < len(rows):
            return rows[args.index]["playlist_url"], rows[args.index]
        print(f"index out of range: {args.index}", file=sys.stderr)
        return None, None
    if args.session:
        hits = [
            (i, r)
            for i, r in enumerate(rows)
            if args.session in r["playlist_url"] or args.session in r["class_id"]
        ]
        if len(hits) == 1:
            return hits[0][1]["playlist_url"], hits[0][1]
        if not hits:
            print(f"no session matches: {args.session}", file=sys.stderr)
            return None, None
        print(f"ambiguous --session {args.session}, use --index:", file=sys.stderr)
        for i, row in hits:
            print(f"  {i}  {row['playlist_url']}", file=sys.stderr)
        return None, None
    print("need --playlist, --session or --index", file=sys.stderr)
    for i, row in enumerate(rows):
        print(f"  {i}  {row['playlist_url']}", file=sys.stderr)
    return None, None


def get_playlist(args, reader, url):
    order = ["cdn", "cache"] if args.refresh else ["cache", "cdn"]
    for how in order:
        if how == "cache":
            if reader is None:
                continue
            entry = reader.find(url)
            data = reader.read_body(entry) if entry else None
            if data:
                return data.decode("utf-8", "replace"), "cache"
            continue
        try:
            return http_get(url, args.timeout).decode("utf-8", "replace"), "cdn"
        except Exception as exc:
            print(f"cdn fetch failed: {exc}", file=sys.stderr)
    return None, ""


def build_session(args, reader, url, text, source):
    parsed = urllib.parse.urlparse(url)
    playlist_dir = posixpath.dirname(parsed.path) + "/"
    session_dir = mirror_dir(args.root, playlist_dir)
    os.makedirs(session_dir, exist_ok=True)

    source_name = urllib.parse.unquote(posixpath.basename(parsed.path)) or SOURCE_M3U8
    source_tmp = os.path.join(session_dir, source_name + ".tmp")
    with open(source_tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(source_tmp, os.path.join(session_dir, source_name))

    header, blocks = parse_blocks(text)
    map_file = os.path.join(session_dir, MAP_NAME)
    lock_path = os.path.join(session_dir, LOCK_NAME)
    locked = acquire_lock(lock_path)
    try:
        return _build_session_locked(
            args, reader, url, text, source, session_dir, header, blocks, map_file
        )
    finally:
        if locked:
            release_lock(lock_path)


def _build_session_locked(args, reader, url, text, source, session_dir, header, blocks, map_file):
    playlist_dir = posixpath.dirname(urllib.parse.urlparse(url).path) + "/"
    data = {"segments": {}, "next": 1}
    if os.path.exists(map_file):
        try:
            data = load_map(map_file)
        except (OSError, ValueError) as exc:
            print(f"existing map unreadable, starting fresh: {exc}", file=sys.stderr)
            data = {"segments": {}, "next": 1}
    segments = data["segments"]
    assets = data.get("assets")
    if not isinstance(assets, dict):
        assets = {}
        data["assets"] = assets

    def localize_asset(match, tag):
        uri = match.group(1)
        if uri.startswith(("data:", "skd:")):
            return match.group(0)
        absolute = urllib.parse.urljoin(url, uri)
        parsed_abs = urllib.parse.urlparse(absolute)
        if parsed_abs.netloc == urllib.parse.urlparse(url).netloc:
            key = urllib.parse.unquote(parsed_abs.path)
        else:
            key = absolute  # different host (e.g. key server): fetch by full URL
        name = assets.get(key)
        if name is None:
            kind = "init" if tag == "#EXT-X-MAP" else "key"
            ext = os.path.splitext(key)[1] or (".mp4" if kind == "init" else ".key")
            name = f"{kind}_{len(assets) + 1:03d}{ext}"
            assets[key] = name
        return f'URI="{name}"'

    def rewrite_tags(lines):
        for i, line in enumerate(lines):
            tag = line.split(":", 1)[0]
            if tag in ASSET_TAGS:
                lines[i] = URI_ATTR_RE.sub(lambda m: localize_asset(m, tag), line)

    rewrite_tags(header)
    for block in blocks:
        rewrite_tags(block)
    added = 0
    for block in blocks:
        key = urllib.parse.unquote(
            urllib.parse.urlparse(urllib.parse.urljoin(url, block[-1])).path
        )
        name = segments.get(key)
        if name is None:
            ext = os.path.splitext(key)[1] or ".ts"
            name = f"{data['next']:0{args.pad}d}{ext}"
            segments[key] = name
            data["next"] += 1
            added += 1
        block[-1] = name

    endlist = "#EXT-X-ENDLIST" in text
    seq = SEQ_RE.search(text)
    durs = [float(x) for x in EXTINF_RE.findall(text)]
    info = None
    if reader is not None:
        class_dir = posixpath.dirname(playlist_dir.rstrip("/"))
        for u, entry in reader.url_entries().items():
            if INFO_NAME not in u:
                continue
            if posixpath.dirname(urllib.parse.urlparse(u).path) != class_dir:
                continue
            candidate = decode_json(reader.read_body(entry))
            if not isinstance(candidate, dict):
                continue
            if info is None or candidate.get("recordTime", 0) > info.get("recordTime", 0):
                info = candidate
    class_id = plaso_titles.class_id_of_dir(playlist_dir)
    tmeta = plaso_titles.resolve_all(args.root).get(class_id) or {}
    title = (getattr(args, "title", "") or "").strip() or tmeta.get("title") or ""
    data.update(
        {
            "version": 2,
            "video_id": playlist_dir.strip("/"),
            "class_id": class_id,
            "title": title,
            "teacher": tmeta.get("createdBy") or tmeta.get("teacher") or "",
            "playlist_url": url,
            "playlist_dir": playlist_dir,
            "playlist_source": source,
            "endlist": endlist,
            "media_sequence": int(seq.group(1)) if seq else None,
            "segment_count": len(blocks),
            "duration": round(sum(durs), 3),
            "info": info,
            "updated_at": int(time.time()),
        }
    )
    save_map(map_file, data)

    lines = ["#EXTM3U"]
    lines.extend(l for l in header if l != "#EXTM3U")
    for block in blocks:
        lines.extend(block)
    if endlist:
        lines.append("#EXT-X-ENDLIST")
    tmp = os.path.join(session_dir, LOCAL_M3U8 + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, os.path.join(session_dir, LOCAL_M3U8))

    log(
        session_dir,
        f"init: {len(blocks)} segments ({added} new), endlist {endlist}, "
        f"duration {sum(durs):.3f}s, playlist from {source}",
    )
    log(session_dir, f"init: title {title or '(unknown)'}")
    log(session_dir, f"init: session {session_dir}")
    return session_dir, data


def cmd_init(args):
    reader = None if args.no_cache else open_cache(args, quiet=True)
    url = args.playlist
    row = None
    if not url:
        if reader is None:
            reader = open_cache(args)
            if reader is None:
                return 1
        url, row = pick_playlist(args, reader)
        if not url:
            reader.close()
            return 1
    text, source = get_playlist(args, reader, url)
    if reader is not None:
        reader.close()
    if text is None:
        print(f"playlist unavailable: {url}", file=sys.stderr)
        return 1
    reader = None if args.no_cache else open_cache(args, quiet=True)
    session_dir, _ = build_session(args, reader, url, text, source)
    if reader is not None:
        reader.close()
    if row and row.get("cached_ts") is not None:
        log(session_dir, f"init: {row['cached_ts']} segments already in client cache")
    return 0


def open_session(args):
    map_file = find_map(args.dir)
    if not map_file:
        print(f"map not found from: {args.dir}", file=sys.stderr)
        return None, None, None, None
    session_dir = os.path.dirname(map_file)
    try:
        data = load_map(map_file)
    except (OSError, ValueError) as exc:
        print(f"map unreadable: {exc}", file=sys.stderr)
        return None, None, None, None
    return map_file, session_dir, data, media_paths(data["segments"])


def cmd_fetch(args):
    found = open_session(args)
    if found[0] is None:
        return 1
    map_file, session_dir, data, paths = found
    if args.only:
        keep = parse_ranges(args.only)
        paths = [p for p in paths if num_of(data["segments"][p]) in keep]
        if not paths:
            print(f"no segments match --only {args.only}", file=sys.stderr)
            return 1
    if args.limit > 0:
        paths = paths[: args.limit]
    log(session_dir, f"segments {len(paths)}, session {session_dir}")
    return fill(args, map_file, session_dir, data, paths)


def cmd_verify(args):
    found = open_session(args)
    if found[0] is None:
        return 1
    _, session_dir, data, paths = found
    log(session_dir, f"segments {len(paths)}, session {session_dir}")
    return verify(session_dir, data, paths, args)


def cmd_merge(args):
    found = open_session(args)
    if found[0] is None:
        return 1
    _, session_dir, data, paths = found
    names = [data["segments"][p] for p in paths]
    out = find_out(session_dir, data.get("title"), args.out)
    try:
        merge(
            session_dir,
            out,
            args.force,
            names=names,
            total_dur=data.get("duration") or 0.0,
            tc=tc_from_args(args),
        )
    except (FileNotFoundError, FileExistsError, RuntimeError) as exc:
        print(f"merge failed: {exc}", file=sys.stderr)
        return 1
    return 0


def add_common(p):
    p.add_argument("--cache-dir", default=DEFAULT_CACHE)
    p.add_argument("--base", default=DEFAULT_BASE)
    p.add_argument("--root", default=os.environ.get("PLASO_DIR", DEFAULT_ROOT))
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--timeout", type=int, default=30)


def add_fill(p):
    p.add_argument("--no-cdn", action="store_true")
    p.add_argument("--mirror", action="store_true")
    p.add_argument("--delay-min", type=float, default=0.5)
    p.add_argument("--delay-max", type=float, default=2.0)
    p.add_argument("--workers", type=int, default=2)
    p.add_argument("--retries", type=int, default=2)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--only", default="")
    p.add_argument("--watch", type=int, default=0)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--merge", action="store_true")
    p.add_argument("--out", default="")
    p.add_argument("--force", action="store_true")
    add_transcode_args(p)


def cmd_titles(args):
    found = plaso_titles.scan_logs()
    if args.write:
        mapping = plaso_titles.load_overrides(args.root)
        for cls, rec in found.items():
            entry = mapping.get(cls)
            entry = dict(entry) if isinstance(entry, dict) else {}
            if not entry.get("title"):
                entry["title"] = rec.get("title", "")
            entry.setdefault("class_id", cls)
            mapping[cls] = entry
        path = plaso_titles.save_overrides(args.root, mapping)
        print(f"wrote {path} ({len(mapping)} entries)")
    if args.json:
        print(json.dumps(found, ensure_ascii=False, indent=1))
        return 0
    rows = [
        [cls, rec.get("title", ""), rec.get("org", ""), rec.get("source", "")]
        for cls, rec in sorted(found.items())
    ]
    if not rows:
        print("no titles found in client logs")
        return 0
    print_table(["class_id", "课程名称", "org", "来源"], rows)
    return 0


def main():
    parser = argparse.ArgumentParser(prog="plaso_get")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("discover")
    add_common(p)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("init")
    add_common(p)
    p.add_argument("--playlist", default="")
    p.add_argument("--session", default="")
    p.add_argument("--index", type=int)
    p.add_argument("--refresh", action="store_true")
    p.add_argument("--title", default="")
    p.add_argument("--pad", type=int, default=int(os.environ.get("PLASO_PAD", "5")))
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("fetch")
    add_common(p)
    add_fill(p)
    p.add_argument("--dir", default=os.getcwd())
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("verify")
    add_common(p)
    p.add_argument("--dir", default=os.getcwd())
    p.add_argument("--out", default="")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("merge")
    add_common(p)
    add_transcode_args(p)
    p.add_argument("--dir", default=os.getcwd())
    p.add_argument("--out", default="")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_merge)

    p = sub.add_parser("titles")
    add_common(p)
    p.add_argument("--json", action="store_true")
    p.add_argument("--write", action="store_true")
    p.set_defaults(func=cmd_titles)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    if sys.platform == "win32":
        import signal

        signal.signal(signal.SIGBREAK, signal.default_int_handler)
    sys.exit(main())
