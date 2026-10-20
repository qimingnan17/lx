# lx

伯索（Plaso）课程回放提取工具。来源优先级：**抓包 / 客户端缓存 → CDN 兜底**。

## 文件

| 文件 | 作用 |
| --- | --- |
| `plaso_get.py` | 命令行入口：`discover` / `init` / `fetch` / `verify` / `merge` / `titles` |
| `plaso_pipeline.py` | 补段、校验、合并的核心逻辑 |
| `plaso_cache.py` | 读取 Chromium 磁盘缓存（`AppData/Roaming/plaso_client/Cache/Cache_Data`） |
| `plaso_gui.py` | Tk 图形界面，所有操作都调用 `plaso_get.py` |
| `plaso_reqable_save.py` | Reqable 抓包脚本，实时保存 m3u8 / 分片 |
| `plaso_titles.py`, `plaso_reqable_titles.py` | 从客户端日志 / 抓包中解析课程名 |
| `merge_plaso.ps1` | 独立的 PowerShell 合并脚本 |

## 常用流程

```bash
python plaso_get.py discover                 # 列出缓存里的课程
python plaso_get.py init --index 0           # 建立会话目录和 local.m3u8
python plaso_get.py fetch --dir <会话目录> --merge
python plaso_get.py verify --dir <会话目录>
```

需要 `ffmpeg` / `ffprobe` 在 PATH 中。

## CDN 兜底的低调设置

- 默认 2 个并发，每次请求前随机等待 0.5–2 秒（`--workers`、`--delay-min`、`--delay-max`）。
- 请求头模拟桌面客户端（Electron 24 / Chromium 112），不带 `Cache-Control: no-cache`。
- 备用域名 `filecdn-t.plaso.com` 默认不用，加 `--mirror` 才在主域名失败时尝试。
- `--no-cdn` 完全只用抓包和缓存。
- 直连，不走系统代理。

## 支持的流

- 普通 TS：concat 合并。
- AES-128 加密（`#EXT-X-KEY`）或 fMP4（`#EXT-X-MAP`）：`init` 会把密钥 / init 段下载到本地并改写 `local.m3u8`，合并时走 ffmpeg 的 HLS 解析。
