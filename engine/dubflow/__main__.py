"""dubflow engine entrypoint."""
import sys

# Windows consoles default to legacy code pages (GBK etc.) and crash on
# CJK output from logs/errors. Force UTF-8 with replacement before anything.
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None:
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

from .main import main

main()
