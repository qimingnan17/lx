"""Single entry point for the packaged program.

    Plaso.exe                -> GUI
    Plaso.exe get <args>     -> plaso_get.py <args>
    Plaso.exe capture <args> -> plaso_capture.py <args>
"""

import os
import sys


def _fix_stdio():
    # A windowed exe has no console; keep print() from crashing when nothing
    # is attached, and use UTF-8 when the GUI reads us through a pipe.
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        if stream is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
        else:
            try:
                stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
            except (AttributeError, ValueError):
                pass


def main():
    _fix_stdio()
    if sys.platform == "win32":
        import signal

        signal.signal(signal.SIGBREAK, signal.default_int_handler)
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "get":
        import plaso_get

        sys.argv = ["plaso_get"] + sys.argv[2:]
        return plaso_get.main()
    if cmd == "capture":
        import plaso_capture

        return plaso_capture.main(sys.argv[2:])
    import plaso_gui

    return plaso_gui.main()


if __name__ == "__main__":
    sys.exit(main())
