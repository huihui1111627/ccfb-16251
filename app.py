"""Convenience entry point: ``python3 app.py`` starts the dashboard."""

import os
import sys

from psdb.__main__ import main

if __name__ == "__main__":
    port = os.environ.get("PORT", "8766")
    raise SystemExit(main(["web", "--port", port]))
