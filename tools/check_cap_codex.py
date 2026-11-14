#!/usr/bin/env python3
"""One real CLI provider decision from an explicitly public request (no rollout)."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from datafarm.cap.codex_provider import build_provider

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--request',type=Path,required=True)
parser.add_argument('--config',type=Path,required=True)
args=parser.parse_args()
decision=build_provider(json.loads(args.config.read_text(encoding='utf-8'))).decide(
    json.loads(args.request.read_text(encoding='utf-8')))
print(json.dumps({'action':decision.get('action'),'code_characters':len(decision.get('code') or ''),
                  'scope':'CLI decision only; no physical execution or task success claim'}))
