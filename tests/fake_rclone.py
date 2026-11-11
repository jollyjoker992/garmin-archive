#!/usr/bin/env python3
"""Minimal stand-in for rclone in tests. Remotes are plain local folders.

Supports `copyto SRC DST [flags]`, `copy SRC DST --files-from LIST` and `lsjson --recursive --files-only DIR`,
with rclone's exit code 3 for a missing directory or file (as the real rclone
returns on the local backend; the tool also accepts 4).
Every call is appended to $FAKE_RCLONE_LOG. $FAKE_RCLONE_FAIL=1 makes every
call fail with exit code 1.
"""
import json
import os
import shutil
import sys
from pathlib import Path

args = [a for a in sys.argv[1:]]
log = os.environ.get("FAKE_RCLONE_LOG")
if log:
    with open(log, "a") as fh:
        fh.write(" ".join(args) + "\n")
if os.environ.get("FAKE_RCLONE_FAIL"):
    print("ERROR : simulated failure", file=sys.stderr)
    sys.exit(1)

positional = []
skip = False
for a in args[1:]:
    if skip:
        skip = False
        continue
    if a.startswith("--"):
        skip = a in ("--retries", "--low-level-retries", "--s3-storage-class", "--files-from",
                     "--transfers", "--checkers")
        continue
    positional.append(a)

if args[0] == "copyto":
    src, dst = Path(positional[0]), Path(positional[1])
    if not src.parent.is_dir():
        sys.exit(3)
    if not src.is_file():
        # Real rclone exits 3 on the local backend and 0 (writing nothing) on S3.
        sys.exit(int(os.environ.get("FAKE_RCLONE_MISSING_EXIT", "3")))
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
elif args[0] == "copy":
    src_root, dst_root = Path(positional[0]), Path(positional[1])
    files = Path(args[args.index("--files-from") + 1]).read_text().split()
    for rel in files:
        if os.environ.get("FAKE_RCLONE_SKIP") and os.environ["FAKE_RCLONE_SKIP"] in rel:
            continue                      # simulate a file the provider refused
        dst = dst_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src_root / rel, dst)
    if os.environ.get("FAKE_RCLONE_SKIP"):
        sys.exit(1)
elif args[0] == "lsjson":
    root = Path(positional[0])
    if not root.is_dir():
        sys.exit(3)
    print(json.dumps([{"Path": str(p.relative_to(root)), "Size": p.stat().st_size}
                      for p in sorted(root.rglob("*")) if p.is_file()]))
else:
    sys.exit(f"fake rclone: unsupported command {args[0]}")
