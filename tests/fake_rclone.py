#!/usr/bin/env python3
"""Minimal stand-in for rclone in tests. Remotes are plain local folders.

Supports `copyto SRC DST [flags]`, `copy SRC DST --files-from LIST` and `lsjson --recursive --files-only DIR`,
with rclone's exit code 3 for a missing directory or file (as the real rclone
returns on the local backend; the tool also accepts 4).
`config dump`, `config file`, `obscure -` and `lsf DIR` work on the INI file
named by $RCLONE_CONFIG; a `name:path` argument whose section has type crypt
resolves to `<its remote>/path` (nothing is really encrypted).
Every call is appended to $FAKE_RCLONE_LOG. $FAKE_RCLONE_FAIL=1 makes every
call fail with exit code 1.
"""
import configparser
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

conf = configparser.ConfigParser()
if os.environ.get("RCLONE_CONFIG"):
    conf.read(os.environ["RCLONE_CONFIG"])


def resolve(arg):
    name, sep, rest = arg.partition(":")
    if sep and conf.has_section(name) and conf[name].get("type") == "crypt":
        return str(Path(conf[name]["remote"]) / rest)
    return arg


if args[0] == "config":
    if args[1] == "dump":
        print(json.dumps({name: dict(conf[name]) for name in conf.sections()}))
    elif args[1] == "file":
        print("Configuration file is stored at:\n" + os.environ["RCLONE_CONFIG"])
    sys.exit(0)
if args[0] == "obscure":
    print("obscured-" + sys.stdin.read().strip()[::-1])
    sys.exit(0)

positional = []
skip = False
for a in args[1:]:
    if skip:
        skip = False
        continue
    if a.startswith("--"):
        skip = a in ("--retries", "--low-level-retries", "--s3-storage-class", "--files-from",
                     "--transfers", "--checkers", "--max-depth")
        continue
    positional.append(resolve(a))

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
elif args[0] == "lsf":
    root = Path(positional[0])
    if not root.is_dir():
        sys.exit(3)
    print("\n".join(sorted(p.name + ("/" if p.is_dir() else "") for p in root.iterdir())))
else:
    sys.exit(f"fake rclone: unsupported command {args[0]}")
