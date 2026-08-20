"""Compatibility entry — prefer `python main.py`.

Kept so older scripts / habits (`python app.py`) and docs still work.
Delegates entirely to main.py (including --engine-worker for frozen builds).
"""

from __future__ import annotations

import os
import runpy
import sys

if __name__ == "__main__":
    main_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py")
    # Execute main.py as __main__ so its --engine-worker gate and main() run unchanged.
    sys.argv[0] = main_path
    runpy.run_path(main_path, run_name="__main__")
