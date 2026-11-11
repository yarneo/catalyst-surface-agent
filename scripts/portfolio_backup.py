"""Private state backup plus hash-verified restore drill; never uploads data."""
from __future__ import annotations
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
from trading_bot.runtime_state import DATA, atomic_json, execution_lock
from trading_bot.sessions import iso_now


def backup(root, destination):
    root, destination = Path(root), Path(destination)
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination.chmod(0o700)
    stamp = iso_now().replace(":", "").replace("+", "_")
    output = destination / ("portfolio-" + stamp + ".tar.gz")
    paths = [p for p in (root / "data").rglob("*") if p.is_file()
             and p.suffix in {".json", ".jsonl"} and "releases" not in p.parts]
    paths += [p for p in root.glob(".env*") if p.name != ".env.example" and p.is_file()]
    manifest = {}
    with execution_lock(root / "data/portfolio.lock"):
        with tarfile.open(output, "w:gz") as archive:
            for p in paths:
                if p.is_symlink():
                    raise ValueError("symlink in private backup")
                if p.name.startswith(".env"):
                    p.chmod(0o600)
                value = p.read_bytes()
                rel = p.relative_to(root).as_posix()
                manifest[rel] = hashlib.sha256(value).hexdigest()
                item = tarfile.TarInfo(rel)
                item.size, item.mode = len(value), 0o600
                archive.addfile(item, io.BytesIO(value))
            payload = json.dumps(manifest, sort_keys=True).encode()
            item = tarfile.TarInfo("backup-manifest.json")
            item.size, item.mode = len(payload), 0o600
            archive.addfile(item, io.BytesIO(payload))
    output.chmod(0o600)
    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(output) as archive:
            archive.extractall(tmp, filter="data")
        for rel, digest in manifest.items():
            if hashlib.sha256((Path(tmp) / rel).read_bytes()).hexdigest() != digest:
                raise RuntimeError("restore verification failed")
    status = {"status": "RESTORE_VERIFIED", "at": iso_now(), "files": len(manifest),
              "archive_sha256": hashlib.sha256(output.read_bytes()).hexdigest()}
    atomic_json(root / "data/backup_health.json", status)
    # Keep the latest 14 verified daily archives; off-host disk snapshots are
    # configured separately by the deployment operator.
    for old in sorted(destination.glob("portfolio-*.tar.gz"))[:-14]:
        old.unlink()
    return status


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--destination", type=Path, default=DATA.parent.parent / "private-backups")
    args = p.parse_args()
    print(json.dumps(backup(DATA.parent, args.destination)))
