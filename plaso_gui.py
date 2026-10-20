import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

HERE = os.path.dirname(os.path.abspath(__file__))
GET_SCRIPT = os.path.join(HERE, "plaso_get.py")
CONFIG_PATH = os.path.join(HERE, "plaso_gui.json")
PYTHON = sys.executable or "python"
DEFAULT_ROOT = os.environ.get("PLASO_DIR") or os.path.join(
    os.path.expanduser("~"), "Downloads", "plaso"
)
CREATE_FLAGS = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0

RE_TOTAL = re.compile(r"segments (\d+),")
RE_SUMMARY = re.compile(r"existing (\d+), cache (\d+), missing (\d+)")
RE_FILL = re.compile(r"(?:cache fill|cdn fill): ")
RE_SESSION = re.compile(r"init: session (.+)$")
RE_PCT = re.compile(r"merge: (\d+)%")
RE_WATCH = re.compile(r"watch: existing (\d+), cache (\d+), missing (\d+)")
RE_SOURCES = re.compile(
    r"sources: capture/existing (\d+), cache (\d+), cdn (\d+), failed (\d+)"
)


def child_env():
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    return env


def human_duration(sec):
    if not sec:
        return "-"
    total = int(sec)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class App:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.proc = None
        self.running = False
        self.rows = []
        self.total = 0
        self.done = 0
        self.started = 0.0
        self.line_count = 0
        self.last_sources = None

        root.title("Plaso 课程抓取控制台")
        root.geometry("1180x760")
        root.minsize(940, 620)

        self.v_root = tk.StringVar(value=DEFAULT_ROOT)
        self.v_dir = tk.StringVar()
        self.v_out = tk.StringVar()
        self.v_only = tk.StringVar()
        self.v_title = tk.StringVar()
        self.v_workers = tk.IntVar(value=2)
        self.v_interval = tk.IntVar(value=300)
        self.v_tc = tk.BooleanVar(value=False)
        self.v_codec = tk.StringVar(value="libx264")
        self.v_preset = tk.StringVar(value="veryfast")
        self.v_crf = tk.IntVar(value=23)
        self.v_scale = tk.StringVar(value="原样")
        self.v_abr = tk.StringVar(value="128k")
        self.v_watch = tk.BooleanVar(value=False)
        self.v_no_cdn = tk.BooleanVar(value=False)
        self.v_no_cache = tk.BooleanVar(value=False)
        self.v_dry = tk.BooleanVar(value=False)
        self.v_merge = tk.BooleanVar(value=False)
        self.v_force = tk.BooleanVar(value=True)
        self.v_refresh = tk.BooleanVar(value=False)
        self.v_status = tk.StringVar(value="就绪")

        self._build()
        self._load_config()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(80, self._poll)
        self.refresh()

    def _build(self):
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Treeview", rowheight=22)

        top = ttk.LabelFrame(self.root, text="课程列表（来自客户端缓存，只读扫描）")
        top.pack(fill="x", padx=10, pady=(10, 6))

        cols = ("idx", "title", "dur", "res", "cache", "local", "state", "class")
        heads = {
            "idx": ("#", 36, "center"),
            "title": ("课程名称", 250, "w"),
            "dur": ("时长", 76, "e"),
            "res": ("分辨率", 84, "center"),
            "cache": ("缓存段", 64, "e"),
            "local": ("本地", 84, "center"),
            "state": ("状态", 96, "center"),
            "class": ("class_id", 240, "w"),
        }
        self.tree = ttk.Treeview(top, columns=cols, show="headings", height=6)
        for key in cols:
            text, width, anchor = heads[key]
            self.tree.heading(key, text=text)
            self.tree.column(
                key, width=width, anchor=anchor, stretch=key in ("title", "class")
            )
        vsb = ttk.Scrollbar(top, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=6)
        vsb.pack(side="left", fill="y", pady=6)
        self.tree.tag_configure("done", foreground="#0a7d32")
        self.tree.tag_configure("part", foreground="#9a6700")
        self.tree.tag_configure("none", foreground="#666666")
        self.tree.bind("<Double-1>", self._on_pick)

        bar = ttk.Frame(top)
        bar.pack(side="left", fill="y", padx=8, pady=6)
        ttk.Button(bar, text="刷新", width=12, command=self.refresh).pack(pady=2)
        ttk.Button(bar, text="选中课程", width=12, command=self._on_pick).pack(pady=2)
        ttk.Button(bar, text="初始化", width=12, command=self.init_selected).pack(pady=2)
        ttk.Button(bar, text="打开目录", width=12, command=self.open_dir).pack(pady=2)

        mid = ttk.LabelFrame(self.root, text="任务参数")
        mid.pack(fill="x", padx=10, pady=6)

        row1 = ttk.Frame(mid)
        row1.pack(fill="x", padx=8, pady=(8, 4))
        ttk.Label(row1, text="课程目录").pack(side="left")
        ttk.Entry(row1, textvariable=self.v_dir).pack(
            side="left", fill="x", expand=True, padx=6
        )
        ttk.Button(row1, text="浏览…", command=self.pick_dir).pack(side="left")

        row2 = ttk.Frame(mid)
        row2.pack(fill="x", padx=8, pady=4)
        ttk.Label(row2, text="保存根目录").pack(side="left")
        ttk.Entry(row2, textvariable=self.v_root, width=44).pack(side="left", padx=6)
        ttk.Label(row2, text="补全区间").pack(side="left", padx=(12, 0))
        ttk.Entry(row2, textvariable=self.v_only, width=14).pack(side="left", padx=6)
        ttk.Label(row2, text="课程名称").pack(side="left", padx=(12, 0))
        ttk.Entry(row2, textvariable=self.v_title, width=26).pack(side="left", padx=6)
        ttk.Label(row2, text="留空自动识别").pack(side="left")

        row3 = ttk.Frame(mid)
        row3.pack(fill="x", padx=8, pady=4)
        ttk.Label(row3, text="并发").pack(side="left")
        ttk.Spinbox(row3, from_=1, to=16, width=5, textvariable=self.v_workers).pack(
            side="left", padx=6
        )
        ttk.Checkbutton(row3, text="轮询抽取缓存", variable=self.v_watch).pack(
            side="left", padx=(14, 0)
        )
        ttk.Label(row3, text="间隔秒").pack(side="left", padx=(6, 0))
        ttk.Spinbox(
            row3, from_=10, to=3600, increment=10, width=7, textvariable=self.v_interval
        ).pack(side="left", padx=6)
        ttk.Label(row3, text="输出 mp4").pack(side="left", padx=(14, 0))
        ttk.Entry(row3, textvariable=self.v_out, width=42).pack(side="left", padx=6)
        ttk.Button(row3, text="另存为…", command=self.pick_out).pack(side="left")
        ttk.Label(row3, text="留空则用课程名命名").pack(side="left", padx=(8, 0))

        row4 = ttk.Frame(mid)
        row4.pack(fill="x", padx=8, pady=(4, 8))
        for text, var in (
            ("不用 CDN", self.v_no_cdn),
            ("不用缓存", self.v_no_cache),
            ("试运行", self.v_dry),
            ("完成后合并", self.v_merge),
            ("覆盖已有 mp4", self.v_force),
            ("强制刷新播放列表", self.v_refresh),
        ):
            ttk.Checkbutton(row4, text=text, variable=var).pack(side="left", padx=(0, 14))

        row5 = ttk.Frame(mid)
        row5.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Checkbutton(row5, text="ffmpeg 转码", variable=self.v_tc).pack(side="left")
        ttk.Label(row5, text="编码器").pack(side="left", padx=(12, 0))
        ttk.Combobox(
            row5,
            textvariable=self.v_codec,
            width=11,
            state="readonly",
            values=("libx264", "libx265", "h264_nvenc", "hevc_nvenc", "h264_qsv", "copy"),
        ).pack(side="left", padx=4)
        ttk.Label(row5, text="预设").pack(side="left", padx=(8, 0))
        ttk.Combobox(
            row5,
            textvariable=self.v_preset,
            width=9,
            state="readonly",
            values=(
                "ultrafast",
                "superfast",
                "veryfast",
                "faster",
                "fast",
                "medium",
                "slow",
                "veryslow",
            ),
        ).pack(side="left", padx=4)
        ttk.Label(row5, text="CRF").pack(side="left", padx=(8, 0))
        ttk.Spinbox(row5, from_=0, to=51, width=4, textvariable=self.v_crf).pack(
            side="left", padx=4
        )
        ttk.Label(row5, text="分辨率").pack(side="left", padx=(8, 0))
        ttk.Combobox(
            row5,
            textvariable=self.v_scale,
            width=7,
            state="readonly",
            values=("原样", "1080p", "720p", "480p", "360p"),
        ).pack(side="left", padx=4)
        ttk.Label(row5, text="音频").pack(side="left", padx=(8, 0))
        ttk.Combobox(
            row5,
            textvariable=self.v_abr,
            width=6,
            state="readonly",
            values=("64k", "96k", "128k", "192k", "256k"),
        ).pack(side="left", padx=4)
        ttk.Label(row5, text="不转码则为无损合并").pack(side="left", padx=(12, 0))

        acts = ttk.Frame(self.root)
        acts.pack(fill="x", padx=10, pady=4)
        self.btn_fetch = ttk.Button(acts, text="抓取 / 补全", command=self.fetch)
        self.btn_fetch.pack(side="left")
        self.btn_stop = ttk.Button(acts, text="停止", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", padx=6)
        self.btn_verify = ttk.Button(acts, text="完整性校验", command=self.verify)
        self.btn_verify.pack(side="left", padx=6)
        self.btn_merge = ttk.Button(acts, text="合并 mp4", command=self.merge)
        self.btn_merge.pack(side="left", padx=6)
        ttk.Button(acts, text="清空日志", command=self.clear_log).pack(side="right")
        ttk.Button(acts, text="保存日志", command=self.save_log).pack(side="right", padx=6)

        prog = ttk.Frame(self.root)
        prog.pack(fill="x", padx=10, pady=(2, 4))
        self.progress = ttk.Progressbar(prog, maximum=100, value=0)
        self.progress.pack(side="left", fill="x", expand=True)
        self.v_ptext = tk.StringVar(value="")
        ttk.Label(prog, textvariable=self.v_ptext, width=22, anchor="e").pack(
            side="left", padx=(8, 0)
        )

        logf = ttk.LabelFrame(self.root, text="日志")
        logf.pack(fill="both", expand=True, padx=10, pady=(4, 4))
        self.log = tk.Text(logf, wrap="none", height=14, state="disabled", font=("Consolas", 10))
        lvsb = ttk.Scrollbar(logf, orient="vertical", command=self.log.yview)
        lhsb = ttk.Scrollbar(logf, orient="horizontal", command=self.log.xview)
        self.log.configure(yscrollcommand=lvsb.set, xscrollcommand=lhsb.set)
        self.log.grid(row=0, column=0, sticky="nsew")
        lvsb.grid(row=0, column=1, sticky="ns")
        lhsb.grid(row=1, column=0, sticky="ew")
        logf.rowconfigure(0, weight=1)
        logf.columnconfigure(0, weight=1)
        self.log.tag_configure("ok", foreground="#0a7d32")
        self.log.tag_configure("err", foreground="#c62828")
        self.log.tag_configure("warn", foreground="#b26a00")
        self.log.tag_configure("gui", foreground="#1565c0")
        self.log.tag_configure("dim", foreground="#888888")

        status = ttk.Frame(self.root)
        status.pack(fill="x", padx=10, pady=(0, 8))
        ttk.Label(status, textvariable=self.v_status, anchor="w").pack(
            side="left", fill="x", expand=True
        )

    def _load_config(self):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except (OSError, ValueError):
            return
        for key, var in (
            ("root", self.v_root),
            ("dir", self.v_dir),
            ("out", self.v_out),
            ("only", self.v_only),
            ("title", self.v_title),
            ("codec", self.v_codec),
            ("preset", self.v_preset),
            ("scale", self.v_scale),
            ("abr", self.v_abr),
        ):
            if isinstance(cfg.get(key), str) and cfg[key]:
                var.set(cfg[key])
        for key, var in (
            ("workers", self.v_workers),
            ("interval", self.v_interval),
            ("crf", self.v_crf),
        ):
            if isinstance(cfg.get(key), int):
                var.set(cfg[key])
        for key, var in (
            ("watch", self.v_watch),
            ("no_cdn", self.v_no_cdn),
            ("no_cache", self.v_no_cache),
            ("dry", self.v_dry),
            ("merge", self.v_merge),
            ("force", self.v_force),
            ("refresh", self.v_refresh),
            ("tc", self.v_tc),
        ):
            if isinstance(cfg.get(key), bool):
                var.set(cfg[key])
        geo = cfg.get("geometry")
        if isinstance(geo, str) and geo:
            self.root.geometry(geo)

    def _save_config(self):
        cfg = {
            "root": self.v_root.get(),
            "dir": self.v_dir.get(),
            "out": self.v_out.get(),
            "only": self.v_only.get(),
            "title": self.v_title.get(),
            "workers": self.v_workers.get(),
            "interval": self.v_interval.get(),
            "crf": self.v_crf.get(),
            "codec": self.v_codec.get(),
            "preset": self.v_preset.get(),
            "scale": self.v_scale.get(),
            "abr": self.v_abr.get(),
            "tc": self.v_tc.get(),
            "watch": self.v_watch.get(),
            "no_cdn": self.v_no_cdn.get(),
            "no_cache": self.v_no_cache.get(),
            "dry": self.v_dry.get(),
            "merge": self.v_merge.get(),
            "force": self.v_force.get(),
            "refresh": self.v_refresh.get(),
            "geometry": self.root.geometry(),
        }
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=1)
        except OSError:
            pass

    def _on_close(self):
        if self.running and self.proc and self.proc.poll() is None:
            if not messagebox.askokcancel(
                "任务进行中", "仍有任务在运行，确定要退出并终止它吗？"
            ):
                return
            self._kill_now()
        self._save_config()
        self.root.destroy()

    def append(self, text, tag=None):
        if tag is None:
            tag = self._tag_for(text)
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n", tag)
        self.line_count += 1
        if self.line_count > 4000:
            self.log.delete("1.0", "500.0")
            self.line_count -= 500
        self.log.see("end")
        self.log.configure(state="disabled")

    def _tag_for(self, text):
        low = text.lower()
        if text.startswith("[gui]"):
            return "gui"
        if "pass" in low or "complete" in low or "merge done" in low:
            return "ok"
        if any(
            k in low
            for k in ("fail", "error", "mismatch", "corrupt", "incomplete", "unavailable")
        ):
            return "err"
        if "warn" in low or "missing" in low or "cache hit" in low:
            return "warn"
        if "cache fill" in low or "cdn fill" in low:
            return "dim"
        return None

    def clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self.line_count = 0

    def save_log(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".log",
            filetypes=[("日志", "*.log"), ("全部文件", "*.*")],
            initialfile=time.strftime("plaso_gui_%Y%m%d_%H%M%S.log"),
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.log.get("1.0", "end"))
        except OSError as exc:
            messagebox.showerror("保存失败", str(exc))
            return
        self.append(f"[gui] 日志已保存: {path}")

    def _set_running(self, on):
        self.running = on
        state = "disabled" if on else "normal"
        for btn in (self.btn_fetch, self.btn_verify, self.btn_merge):
            btn.configure(state=state)
        self.btn_stop.configure(state="normal" if on else "disabled")
        if on:
            self.started = time.time()
            self.total = self.done = 0
            self.last_sources = None
            self.progress.configure(value=0, maximum=100)
            self.v_ptext.set("")

    def pick_out(self):
        row = self._selected_row() or {}
        default = row.get("title") or ""
        if not default:
            default = os.path.basename(self.v_dir.get().strip() or "video")
        initial = os.path.dirname(self.v_out.get().strip()) or self.v_root.get()
        path = filedialog.asksaveasfilename(
            initialdir=initial if os.path.isdir(initial) else self.v_root.get(),
            initialfile=default + ".mp4",
            defaultextension=".mp4",
            filetypes=[("MP4 视频", "*.mp4"), ("MKV 视频", "*.mkv"), ("全部文件", "*.*")],
        )
        if path:
            self.v_out.set(os.path.normpath(path))
            self.append(f"[gui] 输出路径: {self.v_out.get()}")

    def _transcode_args(self):
        if not self.v_tc.get():
            return []
        args = [
            "--transcode",
            "--codec",
            self.v_codec.get(),
            "--preset",
            self.v_preset.get(),
            "--crf",
            str(self.v_crf.get()),
            "--abr",
            self.v_abr.get(),
        ]
        scale = self.v_scale.get()
        if scale and scale != "原样":
            args += ["--scale", scale]
        return args

    def _common_args(self):
        args = ["--root", self.v_root.get().strip() or DEFAULT_ROOT]
        if self.v_no_cache.get():
            args.append("--no-cache")
        return args

    def refresh(self):
        if self.running:
            return
        self.v_status.set("正在扫描客户端缓存…")
        threading.Thread(target=self._discover_worker, daemon=True).start()

    def _discover_worker(self):
        cmd = [PYTHON, GET_SCRIPT, "discover", "--json"] + self._common_args()
        try:
            out = subprocess.run(
                cmd,
                cwd=HERE,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=child_env(),
            )
        except OSError as exc:
            self.q.put(("line", f"[gui] 扫描失败: {exc}"))
            self.q.put(("status", "扫描失败"))
            return
        if out.stderr:
            for line in out.stderr.splitlines():
                self.q.put(("line", line))
        if out.returncode != 0 or not out.stdout.strip():
            self.q.put(("status", f"扫描失败 (code {out.returncode})"))
            return
        try:
            rows = json.loads(out.stdout)
        except ValueError as exc:
            self.q.put(("line", f"[gui] 解析扫描结果失败: {exc}"))
            self.q.put(("status", "扫描失败"))
            return
        self.q.put(("rows", rows))

    def _on_discover(self, rows):
        self.rows = rows
        self.tree.delete(*self.tree.get_children())
        for i, row in enumerate(rows):
            total = row.get("local_total") or 0
            have = row.get("local_have") or 0
            if total and have >= total:
                state, tag = "已完整", "done"
            elif total:
                state, tag = f"部分 {have}/{total}", "part"
            else:
                state, tag = "未下载", "none"
            local = f"{have}/{total}" if total else "-"
            self.tree.insert(
                "",
                "end",
                iid=str(i),
                values=(
                    i,
                    row.get("title") or "(未命名)",
                    human_duration(row.get("duration_s")),
                    row.get("resolution") or "-",
                    row.get("cached_ts", 0),
                    local,
                    state,
                    row.get("class_id", ""),
                ),
                tags=(tag,),
            )
        self.v_status.set(f"发现 {len(rows)} 门课程")

    def _selected_row(self):
        sel = self.tree.selection()
        if not sel:
            return None
        try:
            return self.rows[int(sel[0])]
        except (ValueError, IndexError):
            return None

    def _on_pick(self, _event=None):
        row = self._selected_row()
        if not row:
            self.append("[gui] 请先在列表里选择一门课程")
            return
        local = row.get("local_dir") or ""
        if row.get("title"):
            self.v_title.set(row["title"])
        if local and os.path.exists(os.path.join(local, ".plaso_map.json")):
            self.v_dir.set(os.path.normpath(local))
            self.append(
                f"[gui] 已选择: {row.get('title') or row['class_id']} → {self.v_dir.get()}"
            )
        else:
            self.append(
                f"[gui] {row.get('title') or row['class_id']} 本地还没有目录，"
                "点“初始化”先从缓存建清单"
            )
        self.v_status.set(row.get("playlist_url", ""))

    def pick_dir(self):
        path = filedialog.askdirectory(initialdir=self.v_dir.get() or self.v_root.get())
        if path:
            self.v_dir.set(os.path.normpath(path))

    def open_dir(self):
        path = self.v_dir.get().strip()
        if not path or not os.path.isdir(path):
            row = self._selected_row()
            path = (row or {}).get("local_dir", "")
        if not path or not os.path.isdir(path):
            messagebox.showinfo("提示", "目录还不存在，请先初始化课程")
            return
        try:
            os.startfile(path)  # noqa: S606 - Windows 资源管理器
        except (OSError, AttributeError) as exc:
            messagebox.showerror("打开失败", str(exc))

    def _need_dir(self):
        path = self.v_dir.get().strip()
        if not path:
            messagebox.showinfo("提示", "请先选择或填写课程目录（含 .plaso_map.json 的那一层）")
            return None
        if not os.path.exists(os.path.join(path, ".plaso_map.json")):
            parent = os.path.dirname(path.rstrip("\\/"))
            if parent and os.path.exists(os.path.join(parent, ".plaso_map.json")):
                path = parent
                self.v_dir.set(path)
            elif not os.path.isdir(path):
                messagebox.showinfo("提示", f"目录不存在:\n{path}")
                return None
        return path

    def init_selected(self):
        if self.running:
            return
        row = self._selected_row()
        args = ["init"] + self._common_args()
        if row:
            try:
                idx = self.rows.index(row)
            except ValueError:
                idx = None
            if idx is not None:
                args += ["--index", str(idx)]
        url = self.v_status.get().strip()
        if not row and url.startswith(("http://", "https://")):
            args += ["--playlist", url]
        if not row and not url.startswith(("http://", "https://")):
            messagebox.showinfo("提示", "请先在列表中选择一门课程")
            return
        if self.v_refresh.get():
            args.append("--refresh")
        if self.v_title.get().strip():
            args += ["--title", self.v_title.get().strip()]
        self._stream(args, "init")

    def fetch(self):
        if self.running:
            return
        path = self._need_dir()
        if not path:
            return
        args = ["fetch", "--dir", path] + self._common_args()
        args += ["--workers", str(self.v_workers.get())]
        if self.v_only.get().strip():
            args += ["--only", self.v_only.get().strip()]
        if self.v_watch.get():
            args += ["--watch", str(max(5, self.v_interval.get()))]
        if self.v_no_cdn.get():
            args.append("--no-cdn")
        if self.v_dry.get():
            args.append("--dry-run")
        if self.v_merge.get():
            args.append("--merge")
            if self.v_force.get():
                args.append("--force")
            if self.v_out.get().strip():
                args += ["--out", self.v_out.get().strip()]
            args += self._transcode_args()
        self._stream(args, "fetch")

    def verify(self):
        if self.running:
            return
        path = self._need_dir()
        if not path:
            return
        args = ["verify", "--dir", path] + self._common_args()
        if self.v_out.get().strip():
            args += ["--out", self.v_out.get().strip()]
        self._stream(args, "verify")

    def merge(self):
        if self.running:
            return
        path = self._need_dir()
        if not path:
            return
        args = ["merge", "--dir", path] + self._common_args()
        if self.v_force.get():
            args.append("--force")
        if self.v_out.get().strip():
            args += ["--out", self.v_out.get().strip()]
        args += self._transcode_args()
        self._stream(args, "merge")

    def _stream(self, args, tag):
        if not os.path.exists(GET_SCRIPT):
            messagebox.showerror("缺少文件", f"找不到 {GET_SCRIPT}")
            return
        self._set_running(True)
        self.v_status.set(f"运行中: {' '.join(args[:2])}")
        self.append(f"[gui] $ plaso_get.py {' '.join(args)}")
        threading.Thread(target=self._worker, args=(args, tag), daemon=True).start()

    def _worker(self, args, tag):
        cmd = [PYTHON, GET_SCRIPT] + args
        lines = []
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=HERE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=child_env(),
                creationflags=CREATE_FLAGS,
            )
        except OSError as exc:
            self.q.put(("line", f"[gui] 启动失败: {exc}"))
            self.q.put(("exit", 1, tag, lines))
            return
        self.proc = proc
        try:
            for line in proc.stdout:
                line = line.rstrip("\r\n")
                lines.append(line)
                self.q.put(("line", line))
        except (OSError, ValueError):
            pass
        code = proc.wait()
        self.proc = None
        self.q.put(("exit", code, tag, lines))

    def stop(self):
        proc = self.proc
        if not proc or proc.poll() is not None:
            return
        self.append("[gui] 正在停止（等待子进程收尾，最多 6 秒）…")
        try:
            if os.name == "nt":
                os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
            else:
                proc.send_signal(signal.SIGINT)
        except (OSError, ValueError):
            proc.terminate()
        threading.Thread(target=self._force_kill, args=(proc,), daemon=True).start()

    def _force_kill(self, proc):
        try:
            proc.wait(timeout=6)
        except subprocess.TimeoutExpired:
            proc.kill()
            self.q.put(("line", "[gui] 子进程未响应，已强制结束"))

    def _kill_now(self):
        proc = self.proc
        if proc and proc.poll() is None:
            proc.kill()

    def _progress_line(self, text):
        m = RE_PCT.search(text)
        if m:
            pct = int(m.group(1))
            self.progress.configure(maximum=100, value=pct)
            self.v_ptext.set(f"合并/转码 {pct}%")
            return
        m = RE_SOURCES.search(text)
        if m:
            self.last_sources = tuple(int(x) for x in m.groups())
            return
        m = RE_TOTAL.search(text)
        if m:
            self.total = int(m.group(1))
            self.progress.configure(maximum=max(1, self.total), value=0)
            return
        m = RE_SUMMARY.search(text) or RE_WATCH.search(text)
        if m:
            self.done = int(m.group(1)) + int(m.group(2))
            self._paint_progress()
            return
        if RE_FILL.search(text):
            self.done += 1
            self._paint_progress()

    def _paint_progress(self):
        if self.total <= 0:
            return
        value = min(self.done, self.total)
        self.progress.configure(maximum=self.total, value=value)
        elapsed = time.time() - self.started
        self.v_ptext.set(f"{value}/{self.total}  {elapsed:.0f}s")

    def _handle_exit(self, code, tag, lines):
        self._set_running(False)
        elapsed = time.time() - self.started
        if tag == "init":
            for line in lines:
                m = RE_SESSION.search(line.strip())
                if m:
                    self.v_dir.set(os.path.normpath(m.group(1).strip()))
                    break
        label = {"init": "初始化", "fetch": "抓取", "verify": "校验", "merge": "合并"}.get(
            tag, tag
        )
        state = "成功" if code == 0 else ("已停止" if code < 0 else f"退出码 {code}")
        extra = ""
        if self.last_sources:
            ex, ca, cd, fa = self.last_sources
            extra = f"｜抓包/已有 {ex}，缓存 {ca}，CDN {cd}"
            if fa:
                extra += f"，失败 {fa}"
        self.v_status.set(f"{label}{state}，用时 {elapsed:.1f}s{extra}")
        self.append(f"[gui] {label}结束: {state} ({elapsed:.1f}s)")
        if tag == "verify" and code != 0:
            messagebox.showwarning("校验未通过", "存在缺失或损坏的分片，详见日志")
        if tag in ("fetch", "init") and code == 0:
            self._save_config()
            threading.Thread(target=self._discover_worker, daemon=True).start()

    def _poll(self):
        try:
            while True:
                item = self.q.get_nowait()
                kind = item[0]
                if kind == "line":
                    self.append(item[1])
                    self._progress_line(item[1])
                elif kind == "rows":
                    self._on_discover(item[1])
                elif kind == "status":
                    self.v_status.set(item[1])
                elif kind == "exit":
                    self._handle_exit(item[1], item[2], item[3])
        except queue.Empty:
            pass
        self.root.after(80, self._poll)


def main():
    if not os.path.exists(GET_SCRIPT):
        print(f"missing dependency: {GET_SCRIPT}", file=sys.stderr)
        return 1
    root = tk.Tk()
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        signal.signal(signal.SIGBREAK, signal.default_int_handler)
    sys.exit(main())
