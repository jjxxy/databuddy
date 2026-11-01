#!/usr/bin/env python3
"""Preview (default) or explicitly remove orphaned snapshots for a failed run."""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_service.flow_snapshot_cleanup import cleanup_failed_run

parser = argparse.ArgumentParser(description='预览或清理明确失败运行中未引用的业务流程快照')
parser.add_argument('run_id', help='业务流程运行编号')
parser.add_argument('--apply', action='store_true', help='实际删除预览结果；默认只预览')
args = parser.parse_args()
try:
    print(json.dumps(cleanup_failed_run(args.run_id, apply=args.apply), ensure_ascii=False))
except (ValueError, OSError) as exc:
    print(str(exc), file=sys.stderr)
    sys.exit(1)
