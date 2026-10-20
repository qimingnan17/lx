import glob
import json
import os
import re

TITLE_RE = re.compile(r"ready to play\s*-->\s*title:\s*(.+?),\s*type:")
LOC_RE = re.compile(r"liveclass/([^/\"'\s]+)/(\d+)/([0-9A-Za-z_.\-]+)")
DEFAULT_LOG_DIR = os.path.join(
    os.path.expanduser("~"), "AppData", "Roaming", "plaso_client", "P403FileTemp"
)
OVERRIDE_NAME = "titles.json"
BAD_CHARS = '<>:"/\\|?*'


def sanitize(name, fallback="video"):
    cleaned = "".join("_" if c in BAD_CHARS else c for c in (name or "")).strip(" .")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:120] or fallback


def scan_logs(log_dir=DEFAULT_LOG_DIR, limit_mb=64):
    votes = {}
    meta = {}
    try:
        paths = sorted(glob.glob(os.path.join(log_dir, "*.log")))
    except OSError:
        return {}
    for path in paths:
        try:
            if os.path.getsize(path) > limit_mb * 1024 * 1024:
                continue
            title = None
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    m = TITLE_RE.search(line)
                    if m:
                        title = m.group(1).strip()
                        continue
                    if not title:
                        continue
                    for hit in LOC_RE.finditer(line):
                        loc_path, org, cls = hit.group(1), hit.group(2), hit.group(3)
                        tally = votes.setdefault(cls, {})
                        tally[title] = tally.get(title, 0) + 1
                        meta.setdefault(
                            cls, {"org": org, "location_path": loc_path, "logs": []}
                        )
                        if path not in meta[cls]["logs"]:
                            meta[cls]["logs"].append(path)
        except OSError:
            continue
    found = {}
    for cls, tally in votes.items():
        rec = dict(meta.get(cls, {}))
        rec.update(
            {
                "class_id": cls,
                "title": max(tally.items(), key=lambda kv: kv[1])[0],
                "votes": tally,
                "source": "client-log",
            }
        )
        found[cls] = rec
    return found


def load_overrides(root):
    try:
        with open(os.path.join(root, OVERRIDE_NAME), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_overrides(root, mapping):
    path = os.path.join(root, OVERRIDE_NAME)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(mapping, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    return path


def resolve_all(root, log_dir=DEFAULT_LOG_DIR):
    found = scan_logs(log_dir)
    for cls, rec in load_overrides(root).items():
        if isinstance(rec, str):
            rec = {"title": rec}
        if not isinstance(rec, dict) or not rec.get("title"):
            continue
        merged = dict(found.get(cls, {}))
        merged.update(rec)
        merged["class_id"] = cls
        merged["source"] = OVERRIDE_NAME
        found[cls] = merged
    return found


def title_for(mapping, class_id):
    rec = mapping.get(class_id) or {}
    return rec.get("title") or ""


def class_id_of_dir(playlist_dir):
    parts = [p for p in (playlist_dir or "").split("/") if p]
    if len(parts) >= 2:
        return parts[-2]
    return parts[-1] if parts else ""
