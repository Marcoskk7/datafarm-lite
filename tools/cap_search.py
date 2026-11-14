#!/usr/bin/env python3
"""ASPIRE-inspired author-side search, using the existing expert-harness broker."""
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from datafarm.cap.cli import main

if __name__=='__main__':raise SystemExit(main())
