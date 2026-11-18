#!/usr/bin/env python3
"""Operator bridge for NAS reports; never approves ingestion."""
import argparse
import json
import os
from pathlib import Path
from repair_core import RepairManager, CONFIRM
from reviewer_core import ReviewerStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    prepare = sub.add_parser('prepare')
    prepare.add_argument('document')
    prepare.add_argument('sha256')
    retry = sub.add_parser('retry')
    retry.add_argument('sha256')
    apply = sub.add_parser('apply')
    apply.add_argument('decisions', help='reviewed local JSON file inside container')
    apply.add_argument('--confirm', required=True)
    args = parser.parse_args()
    manager = RepairManager(ReviewerStore(os.environ.get('RAG_ROOT', '/data/rag-store')))
    try:
        if args.command == 'prepare':
            result = manager.prepare(args.document, args.sha256)
        elif args.command == 'retry':
            result = manager.retry(args.sha256)
        else:
            payload = json.loads(Path(args.decisions).read_text())
            result = manager.apply(payload['sha256'], payload['decisions'], payload['reason'], args.confirm)
        print(json.dumps(result, indent=2))
    finally:
        manager.pool.shutdown(wait=True)


if __name__ == '__main__':
    main()
