#!/usr/bin/env python3
from __future__ import annotations
import argparse
from pathlib import Path

def remove(path, dry_run=False):
    if dry_run:
        print(path)
    else:
        path.unlink(missing_ok=True)
        print(f"removed {path}")

def main():
    parser = argparse.ArgumentParser(description="Safe cleanup for a job. Never removes shared model caches.")
    parser.add_argument("--job-dir", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--remove-old-chunk-hashes", action="store_true")
    args = parser.parse_args()
    job = Path(args.job_dir).expanduser().resolve()
    for pattern in ["*.tmp", "fit_src_*.wav", "fit_dst_*.wav", "stdout_bg.log"]:
        for path in job.glob(pattern):
            remove(path, args.dry_run)
    if args.remove_old_chunk_hashes:
        groups = {}
        for path in job.glob("chunk_*_qwen3tts_*.wav"):
            parts = path.stem.split("_")
            if len(parts) >= 5:
                groups.setdefault(tuple(parts[:3]), {}).setdefault(parts[3], []).append(path)
        for hashes in groups.values():
            if len(hashes) <= 1:
                continue
            newest = max(hashes, key=lambda h: max(p.stat().st_mtime for p in hashes[h]))
            for h, paths in hashes.items():
                if h != newest:
                    for path in paths:
                        remove(path, args.dry_run)
    print("safe cleanup complete; shared model caches were not touched")

if __name__ == "__main__":
    main()
