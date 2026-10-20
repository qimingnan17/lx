from reqable import *

import datetime
import json
import os

PATH_HINT = "getfileinfo"
DEFAULT_DIR = os.path.join(os.path.expanduser("~"), "Downloads", "plaso")
FILE_NAME = "titles.json"
LOG_NAME = "titles.log"


def _save_dir(context):
    return os.path.expanduser((context.env or {}).get("PLASO_DIR") or DEFAULT_DIR)


def _log(context, message):
    line = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {message}"
    print(f"[plaso-titles] {message}", flush=True)
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


def onResponse(context, response):
    try:
        return _on_response(context, response)
    except Exception as exc:
        _log(context, f"ERROR {exc}")
        return response


def _on_response(context, response):
    url = context.url or ""
    if PATH_HINT not in url:
        return response
    if response.code != 200 or response.body.isNone or response.body.isMultipart:
        return response
    try:
        data = json.loads(_body_text(response.body))
    except ValueError:
        return response
    obj = data.get("obj") or data.get("file") or {}
    if not isinstance(obj, dict):
        return response
    location = (obj.get("location") or "").strip()
    name = (obj.get("name") or "").strip()
    if not location or not name:
        return response
    class_id = location.rstrip("/").split("/")[-1]

    root = _save_dir(context)
    os.makedirs(root, exist_ok=True)
    path = os.path.join(root, FILE_NAME)
    mapping = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            mapping = loaded
    except (OSError, ValueError):
        mapping = {}

    entry = mapping.get(class_id)
    entry = dict(entry) if isinstance(entry, dict) else {}
    entry.update(
        {
            "title": name,
            "class_id": class_id,
            "location": location,
            "teacher": obj.get("createdBy") or "",
            "duration_s": obj.get("duration"),
            "size": obj.get("size"),
            "file_id": obj.get("_id") or "",
            "record_id": obj.get("recordId") or "",
            "created_at": obj.get("createdAt") or "",
            "source": "api",
        }
    )
    mapping[class_id] = entry

    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(mapping, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    _log(context, f"title saved: {class_id} -> {name}")
    return response
