#!/usr/bin/env python3
"""Search, probe and fetch NVIDIA SimReady assets (Hugging Face `nvidia/simready-assets`).

    python tools/simready.py --endpoint https://hf-mirror.com search plate --limit 5
    python tools/simready.py search cabinet --require-joint prismatic
    python tools/simready.py probe fixture_cabinet_b01
    python tools/simready.py fetch fixture_cabinet_b01 --dest /data/simready-cache

The endpoint defaults to $HF_ENDPOINT, then https://huggingface.co. Every command pins the
dataset revision to a commit sha and prints it, so a later fetch can reproduce a search.
"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from datafarm.adapters.assets.simready import (SimReadyClient, SimReadyError, endpoint_from_env,
                                               thumbnails_only_skip)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--endpoint', default=endpoint_from_env())
    parser.add_argument('--revision', default='main', help='branch or 40-hex commit sha')
    sub = parser.add_subparsers(dest='command', required=True)
    search = sub.add_parser('search')
    search.add_argument('terms', nargs='+')
    search.add_argument('--limit', type=int, default=10)
    search.add_argument('--require-joint', choices=['prismatic', 'revolute'])
    search.add_argument('--context', nargs='*', default=[], help='terms scored against source collection and joint names')
    search.add_argument('--probe', action='store_true', help='read license, collection and joints of each hit')
    search.add_argument('--min-joints', type=int, default=1)
    probe = sub.add_parser('probe')
    probe.add_argument('assets', nargs='+')
    fetch = sub.add_parser('fetch')
    fetch.add_argument('assets', nargs='+')
    fetch.add_argument('--dest', type=Path, required=True)
    fetch.add_argument('--all-files', action='store_true', help='also download texture preview thumbnails')
    args = parser.parse_args()
    client = SimReadyClient(endpoint=args.endpoint, revision=args.revision)
    try:
        revision = client.pin()
        if args.command == 'search':
            found = client.search(args.terms, context=args.context, limit=args.limit, probe=args.probe,
                                  require_joint=args.require_joint, min_joints=args.min_joints)
            out = {'revision': revision, 'terms': args.terms, 'results': [c.to_dict() for c in found]}
        elif args.command == 'probe':
            out = {'revision': revision, 'results': [client.probe(a).to_dict() for a in args.assets]}
        else:
            skip = None if args.all_files else thumbnails_only_skip
            out = {'revision': revision, 'results': []}
            for asset in args.assets:
                manifest = client.fetch(asset, args.dest, skip=skip,
                                        log=lambda m: print('fetch', m, file=sys.stderr, flush=True))
                out['results'].append({k: manifest[k] for k in
                                       ('asset', 'package_id', 'license', 'bom_sha256', 'root_usds')}
                                      | {'files': len(manifest['files']), 'skipped': len(manifest['skipped'])})
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0
    except SimReadyError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
