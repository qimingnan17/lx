import argparse
import concurrent.futures
import datetime
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request

from plaso_cache import CacheError, CacheReader
import plaso_titles

DEFAULT_CACHE = os.path.join(
    os.path.expanduser("~"), "AppData", "Roaming", "plaso_client", "Cache", "Cache_Data"
)
DEFAULT_BASE = "https://filecdn.plaso.com"
MAP_NAME = ".plaso_map.json"
LOG_NAME = "plaso_pipeline.log"
TS_PACKET = 188
EXTINF_RE = re.compile(r"#EXTINF:([0-9.]+)")


def log(session_dir, message):
    line = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {message}"
    print(line, flush=True)
    try:
        with open(os.path.join(session_dir, LOG_NAME), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def find_map(start):
    cur = os.path.abspath(start)
    for _ in range(5):
        candidate = os.path.join(cur, MAP_NAME)
        if os.path.exists(candidate):
            return candidate
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return None


def load_map(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    segments = data.get("segments")
    if not isinstance(segments, dict):
        raise ValueError("invalid map: segments missing")
    return data


def save_map(path, data):
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def media_paths(segments):
    exts = (".ts", ".m4s", ".m4a", ".aac", ".mp4", ".key")
    return [p for p in segments if os.path.splitext(p)[1].lower() in exts]


def common_dir(paths):
    dirs = [posixpath.dirname(p) for p in paths]
    if not dirs:
        return "/"
    common = posixpath.commonpath(dirs)
    return common.rstrip("/") + "/"


def ts_intact(path, size):
    if size % TS_PACKET:
        return False
    try:
        with open(path, "rb") as f:
            sync = f.read()[0::TS_PACKET]
    except OSError:
        return False
    return sync == b"\x47" * (size // TS_PACKET)


def valid_media(path, ext):
    try:
        size = os.path.getsize(path)
    except OSError:
        return False
    if size <= 0:
        return False
    if ext.lower() == ".ts":
        return ts_intact(path, size)
    return True


def ranges(numbers):
    if not numbers:
        return ""
    numbers = sorted(numbers)
    parts = []
    start = prev = numbers[0]
    for n in numbers[1:]:
        if n == prev + 1:
            prev = n
            continue
        parts.append(str(start) if start == prev else f"{start}-{prev}")
        start = prev = n
    parts.append(str(start) if start == prev else f"{start}-{prev}")
    return ", ".join(parts)


def playlist_meta(path):
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    uris = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    durs = [float(x) for x in EXTINF_RE.findall(text)]
    return {
        "endlist": "#EXT-X-ENDLIST" in text,
        "uris": uris,
        "duration": sum(durs),
        "max_dur": max(durs) if durs else 0.0,
    }


def probe_duration(path):
    try:
        out = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=nw=1:nk=1",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    try:
        return float(out.stdout.strip())
    except ValueError:
        return None


def default_out(session_dir, title=None):
    class_dir = os.path.dirname(session_dir)
    name = plaso_titles.sanitize(title, "") if title else ""
    return os.path.join(class_dir, (name or os.path.basename(class_dir)) + ".mp4")


def find_out(session_dir, title=None, explicit=""):
    if explicit:
        return explicit
    named = default_out(session_dir, title)
    if os.path.exists(named):
        return named
    class_dir = os.path.dirname(session_dir)
    legacy = os.path.join(class_dir, os.path.basename(class_dir) + ".mp4")
    return legacy if os.path.exists(legacy) else named


def numbered(name):
    base = os.path.splitext(name)[0]
    return int(base) if base.isdigit() else None


def resolve_names(session_dir, names):
    if names:
        return [n for n in names if os.path.exists(os.path.join(session_dir, n))]
    found = [
        f
        for f in os.listdir(session_dir)
        if f.lower().endswith((".ts", ".m4s")) and os.path.isfile(os.path.join(session_dir, f))
    ]
    return sorted(found, key=lambda x: (numbered(x) is None, numbered(x) or 0, x))


def write_concat_list(session_dir, names):
    lines = []
    for name in names:
        path = os.path.abspath(os.path.join(session_dir, name)).replace("\\", "/")
        lines.append("file '" + path.replace("'", "'\\''") + "'")
    list_path = os.path.join(session_dir, ".plaso_concat.txt")
    with open(list_path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    return list_path


def normalize_scale(value):
    text = (value or "").strip()
    if not text:
        return ""
    m = re.fullmatch(r"(\d{3,4})[pP]", text)
    if m:
        return f"-2:{m.group(1)}"
    if "x" in text:
        w, h = text.split("x", 1)
        return f"{w.strip()}:{h.strip()}"
    return text


def ffmpeg_args(tc):
    if not tc or tc.get("codec") == "copy":
        return ["-c", "copy"]
    codec = tc.get("codec") or "libx264"
    args = ["-c:v", codec]
    if codec in ("libx264", "libx265"):
        args += ["-preset", tc.get("preset") or "veryfast", "-crf", str(tc.get("crf", 23))]
    elif codec.endswith("_nvenc"):
        args += ["-preset", "p5", "-cq", str(tc.get("crf", 23))]
    elif codec.endswith("_qsv"):
        args += ["-global_quality", str(tc.get("crf", 23))]
    else:
        args += ["-crf", str(tc.get("crf", 23))]
    vf = []
    if tc.get("scale"):
        vf.append(f"scale={normalize_scale(tc['scale'])}")
    if tc.get("fps"):
        vf.append(f"fps={tc['fps']}")
    if vf:
        args += ["-vf", ",".join(vf)]
    args += ["-c:a", tc.get("acodec") or "aac"]
    if tc.get("abr"):
        args += ["-b:a", tc["abr"]]
    if tc.get("faststart", True):
        args += ["-movflags", "+faststart"]
    return args


PROGRESS_SKIP = (
    "frame=",
    "out_time=",
    "speed=",
    "bitrate=",
    "total_size=",
    "dup_frames=",
    "drop_frames=",
    "progress=",
)


def run_ffmpeg(cmd, session_dir, total_dur, on_progress):
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    tail = []
    last_pct = -5.0
    try:
        for raw in proc.stdout:
            line = raw.strip()
            if not line:
                continue
            if line.startswith("out_time_ms="):
                try:
                    usec = int(line.split("=", 1)[1])
                except ValueError:
                    continue
                if total_dur:
                    pct = min(100.0, usec / 1e6 / total_dur * 100.0)
                    if on_progress:
                        on_progress(pct)
                    elif pct - last_pct >= 5.0:
                        last_pct = pct
                        log(session_dir, f"merge: {pct:.0f}%")
                continue
            if line.startswith("progress=end"):
                if on_progress:
                    on_progress(100.0)
                else:
                    log(session_dir, "merge: 100%")
                continue
            if line.startswith(PROGRESS_SKIP):
                continue
            tail.append(line)
            if len(tail) > 25:
                tail.pop(0)
    except (OSError, ValueError):
        pass
    code = proc.wait()
    return code, tail


def merge(session_dir, out, force, names=None, total_dur=0.0, tc=None, on_progress=None):
    names = resolve_names(session_dir, names)
    if not names:
        raise RuntimeError("no segments to merge")
    nums = sorted(n for n in (numbered(x) for x in names) if n is not None)
    if nums:
        gaps = [i for i in range(nums[0], nums[-1] + 1) if i not in set(nums)]
        if gaps:
            log(session_dir, f"merge: WARNING {len(gaps)} gaps: {ranges(gaps)}")
    if os.path.exists(out) and not force:
        raise FileExistsError(f"output exists: {out}")
    parent = os.path.dirname(os.path.abspath(out))
    if parent:
        os.makedirs(parent, exist_ok=True)

    list_path = write_concat_list(session_dir, names)
    tmp = out + ".part.mp4"
    cmd = (
        ["ffmpeg", "-hide_banner", "-y", "-f", "concat", "-safe", "0", "-i", list_path]
        + ffmpeg_args(tc)
        + ["-progress", "pipe:1", "-nostats", tmp]
    )
    mode = "transcode" if tc and tc.get("codec") != "copy" else "remux"
    log(session_dir, f"merge: {mode} {len(names)} segments -> {out}")
    try:
        code, tail = run_ffmpeg(cmd, session_dir, total_dur, on_progress)
    finally:
        try:
            os.remove(list_path)
        except OSError:
            pass
    if code != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise RuntimeError(f"ffmpeg failed ({code}): " + " | ".join(tail[-6:]))
    os.replace(tmp, out)
    log(
        session_dir,
        f"merge: done {os.path.getsize(out) / 1048576:.2f} MB -> {out}",
    )
    return out


def tc_from_args(args):
    if not getattr(args, "transcode", False):
        return None
    return {
        "codec": args.codec,
        "preset": args.preset,
        "crf": args.crf,
        "scale": args.scale,
        "fps": args.fps,
        "abr": args.abr,
        "acodec": args.acodec,
        "faststart": not args.no_faststart,
    }


def add_transcode_args(parser):
    parser.add_argument("--transcode", action="store_true")
    parser.add_argument("--codec", default="libx264")
    parser.add_argument("--preset", default="veryfast")
    parser.add_argument("--crf", type=int, default=23)
    parser.add_argument("--scale", default="")
    parser.add_argument("--fps", type=int, default=0)
    parser.add_argument("--abr", default="128k")
    parser.add_argument("--acodec", default="aac")
    parser.add_argument("--no-faststart", action="store_true")


def verify(session_dir, data, paths, args):
    ok = True
    segments = data["segments"]
    sizes = data.get("sizes") or {}
    meta = playlist_meta(os.path.join(session_dir, "local.m3u8"))
    names = [segments[p] for p in paths]

    if data.get("title"):
        log(session_dir, f"verify: title {data['title']}")
    if meta is None:
        log(session_dir, "verify: local.m3u8 missing")
        ok = False
    else:
        log(
            session_dir,
            f"verify: playlist {len(meta['uris'])} segments, map {len(paths)}, "
            f"endlist {meta['endlist']}, duration {meta['duration']:.3f}s",
        )
        if len(meta["uris"]) != len(paths):
            log(
                session_dir,
                f"verify: COUNT MISMATCH playlist {len(meta['uris'])} vs map {len(paths)}",
            )
            ok = False
        if meta["uris"] != names:
            first = next(
                (i for i, (a, b) in enumerate(zip(meta["uris"], names)) if a != b),
                min(len(meta["uris"]), len(names)),
            )
            log(session_dir, f"verify: ORDER MISMATCH at index {first}")
            ok = False
        if not meta["endlist"]:
            log(session_dir, "verify: WARNING playlist has no #EXT-X-ENDLIST")
            ok = False

    missing, bad = [], []
    for name in names:
        dest = os.path.join(session_dir, name)
        if not os.path.exists(dest):
            missing.append(name)
            continue
        expected = sizes.get(name)
        if expected and os.path.getsize(dest) != expected:
            bad.append(name)
            continue
        if not valid_media(dest, os.path.splitext(name)[1]):
            bad.append(name)

    if sizes:
        log(session_dir, f"verify: recorded sizes for {len(sizes)} segments")

    for label, group in (("missing", missing), ("corrupt", bad)):
        if not group:
            continue
        ok = False
        nums = []
        for name in group:
            base = os.path.splitext(name)[0]
            if base.isdigit():
                nums.append(int(base))
        detail = f" numbers {ranges(nums)}" if nums else ""
        log(session_dir, f"verify: {len(group)} {label}{detail}")

    total = sum(
        os.path.getsize(os.path.join(session_dir, n))
        for n in names
        if os.path.exists(os.path.join(session_dir, n))
    )
    log(session_dir, f"verify: files {len(names) - len(missing)}/{len(names)}, {total / 1048576:.2f} MB")

    if meta and not missing and not bad:
        out = find_out(session_dir, data.get("title"), args.out)
        if os.path.exists(out):
            dur = probe_duration(out)
            if dur is None:
                log(session_dir, f"verify: ffprobe failed on {out}")
                ok = False
            else:
                tol = max(5.0, meta["max_dur"])
                delta = dur - meta["duration"]
                log(
                    session_dir,
                    f"verify: mp4 {dur:.3f}s vs playlist {meta['duration']:.3f}s "
                    f"(delta {delta:+.3f}s, tol {tol:.3f}s)",
                )
                if abs(delta) > tol:
                    log(session_dir, "verify: DURATION MISMATCH")
                    ok = False
        else:
            log(session_dir, f"verify: mp4 not found: {out}")

    log(session_dir, "verify: PASS" if ok else "verify: FAIL")
    return 0 if ok else 2


def fetch_one(url, dest, timeout, retries):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    last = None
    for attempt in range(retries + 1):
        tmp = dest + ".part"
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Accept": "*/*",
                    "Cache-Control": "no-cache",
                },
            )
            with opener.open(req, timeout=timeout) as resp:
                if resp.status != 200:
                    raise OSError(f"HTTP {resp.status}")
                expected = resp.headers.get("Content-Length")
                with open(tmp, "wb") as f:
                    shutil.copyfileobj(resp, f, 1024 * 256)
            size = os.path.getsize(tmp)
            if size <= 0:
                raise OSError("empty body")
            if expected is not None and int(expected) != size:
                raise OSError(f"size mismatch {size}/{expected}")
            os.replace(tmp, dest)
            return True, None
        except Exception as exc:
            last = exc
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            if attempt < retries:
                continue
    return False, str(last)


def build_cache_map(args, paths, session_dir):
    prefix = args.base + common_dir(paths)
    try:
        cache = CacheReader(args.cache_dir)
    except (OSError, CacheError) as exc:
        log(session_dir, f"cache unavailable: {exc}")
        return None, {}
    cache_map = {}
    for url, entry in cache.url_entries(prefix).items():
        path = urllib.parse.unquote(urllib.parse.urlparse(url).path)
        old = cache_map.get(path)
        if (
            old is None
            or (old["state"] != 0 and entry["state"] == 0)
            or (
                old["state"] == entry["state"] == 0
                and entry["ctime"] > old["ctime"]
            )
        ):
            cache_map[path] = entry
    log(session_dir, f"cache entries {len(cache_map)} under {prefix}")
    return cache, cache_map


def cache_pass(session_dir, segments, paths, cache, cache_map, dry):
    existing = 0
    from_cache = 0
    missing = []
    for path in paths:
        name = segments[path]
        dest = os.path.join(session_dir, name)
        ext = os.path.splitext(name)[1]
        if valid_media(dest, ext):
            existing += 1
            continue
        entry = cache_map.get(path)
        if entry is not None:
            if dry:
                from_cache += 1
                continue
            if cache.copy_body(entry, dest):
                if valid_media(dest, ext):
                    from_cache += 1
                    log(session_dir, f"cache fill: {name}")
                    continue
                try:
                    os.remove(dest)
                except OSError:
                    pass
                log(
                    session_dir,
                    f"cache corrupt: {name} ({entry['data_size'][1]} bytes), will refetch",
                )
        missing.append((path, name, ext))
    return existing, from_cache, missing


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", default=os.getcwd())
    parser.add_argument("--cache-dir", default=DEFAULT_CACHE)
    parser.add_argument("--base", default=DEFAULT_BASE)
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--no-cdn", action="store_true")
    parser.add_argument("--merge", action="store_true")
    parser.add_argument("--out", default="")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--watch", type=int, default=0)
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    add_transcode_args(parser)
    args = parser.parse_args()

    map_path = find_map(args.dir)
    if not map_path:
        print(f"map not found from: {args.dir}", file=sys.stderr)
        return 1
    session_dir = os.path.dirname(map_path)
    data = load_map(map_path)
    segments = data["segments"]
    paths = media_paths(segments)
    if args.limit > 0:
        paths = paths[: args.limit]
    if not paths:
        print("no media segments in map", file=sys.stderr)
        return 1

    log(session_dir, f"segments {len(paths)}, session {session_dir}")

    if args.verify:
        return verify(session_dir, data, paths, args)

    return fill(args, map_path, session_dir, data, paths)


def fill(args, map_path, session_dir, data, paths):
    segments = data["segments"]
    cache = None
    cache_map = {}
    if not args.no_cache:
        cache, cache_map = build_cache_map(args, paths, session_dir)

    if args.watch > 0 and not args.dry_run:
        log(session_dir, f"watch: cache-only pass every {args.watch}s, ctrl+c to stop")
        try:
            while True:
                if cache:
                    cache.close()
                if not args.no_cache:
                    cache, cache_map = build_cache_map(args, paths, session_dir)
                existing, from_cache, missing = cache_pass(
                    session_dir, segments, paths, cache, cache_map, False
                )
                log(
                    session_dir,
                    f"watch: existing {existing}, cache {from_cache}, missing {len(missing)}",
                )
                if not missing:
                    log(session_dir, "watch: all segments present")
                    break
                time.sleep(args.watch)
        except KeyboardInterrupt:
            log(session_dir, "watch: stopped by user")
    else:
        existing, from_cache, missing = cache_pass(
            session_dir, segments, paths, cache, cache_map, args.dry_run
        )

    log(session_dir, f"existing {existing}, cache {from_cache}, missing {len(missing)}")
    if missing:
        nums = []
        for _, name, _ in missing:
            base = os.path.splitext(name)[0]
            if base.isdigit():
                nums.append(int(base))
        if nums:
            log(session_dir, f"missing numbers: {ranges(nums)}")

    if args.dry_run:
        log(session_dir, f"dry-run: would fill {len(missing)} from cdn")
        if cache:
            cache.close()
        return 0

    failed = []
    if missing and not args.no_cdn:
        def work(item):
            path, name, ext = item
            dest = os.path.join(session_dir, name)
            url = args.base + urllib.parse.quote(path, safe="/-_.~")
            ok, err = fetch_one(url, dest, args.timeout, args.retries)
            return path, name, ext, ok, err

        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            for path, name, ext, ok, err in pool.map(work, missing):
                if ok:
                    log(session_dir, f"cdn fill: {name}")
                else:
                    failed.append((name, err))
                    log(session_dir, f"cdn fail: {name}: {err}")
        if failed:
            log(session_dir, f"cdn failed {len(failed)}")

    sizes = data.get("sizes")
    if not isinstance(sizes, dict):
        sizes = {}
        data["sizes"] = sizes
    changed = False
    for path in paths:
        name = segments[path]
        if name in sizes:
            continue
        dest = os.path.join(session_dir, name)
        try:
            size = os.path.getsize(dest)
        except OSError:
            continue
        if size > 0 and valid_media(dest, os.path.splitext(name)[1]):
            sizes[name] = size
            changed = True
    if changed:
        save_map(map_path, data)
        log(session_dir, f"recorded sizes: {len(sizes)}")

    remaining = []
    for path in paths:
        name = segments[path]
        if not valid_media(os.path.join(session_dir, name), os.path.splitext(name)[1]):
            remaining.append(name)

    if cache:
        cache.close()

    if remaining:
        log(session_dir, f"incomplete: {len(remaining)} missing")
        return 2

    log(
        session_dir,
        f"sources: capture/existing {existing}, cache {from_cache}, "
        f"cdn {len(missing) - len(failed)}, failed {len(failed)}",
    )
    log(session_dir, "complete")
    if args.merge:
        all_names = [segments[p] for p in media_paths(segments)]
        absent = [
            n
            for n in all_names
            if not valid_media(os.path.join(session_dir, n), os.path.splitext(n)[1])
        ]
        if absent:
            nums = sorted(n for n in (numbered(x) for x in absent) if n is not None)
            log(
                session_dir,
                f"merge skipped: {len(absent)} segments absent"
                + (f" ({ranges(nums)})" if nums else ""),
            )
        else:
            merge(
                session_dir,
                find_out(session_dir, data.get("title"), args.out),
                args.force,
                names=all_names,
                total_dur=data.get("duration") or 0.0,
                tc=tc_from_args(args),
            )
    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        import signal

        signal.signal(signal.SIGBREAK, signal.default_int_handler)
    sys.exit(main())
