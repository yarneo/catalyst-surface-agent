"""Install verified code under the portfolio lock, preserving private state."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import tarfile
import tempfile
import datetime
import fcntl


def install(archive_path, root):
    root = Path(root).resolve()
    (root / "data").mkdir(exist_ok=True)
    with (root / "data/portfolio.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp)
            with tarfile.open(archive_path) as archive:
                members = archive.getmembers()
                for entry in members:
                    p = Path(entry.name)
                    if not entry.isfile() or p.is_absolute() or ".." in p.parts:
                        raise ValueError("unsafe archive member")
                    if entry.name != "release-manifest.json" and p.parts[0] not in {"src", "web", "scripts"}:
                        raise ValueError("release attempted to overwrite private state")
                    if any(part.startswith(".env") for part in p.parts):
                        raise ValueError("credentials in release")
                archive.extractall(stage, filter="data")
            manifest = json.loads((stage / "release-manifest.json").read_text())
            if {m.name for m in members} != set(manifest["files"]) | {"release-manifest.json"}:
                raise ValueError("unmanifested release contents")
            for rel, expected in manifest["files"].items():
                source = stage / rel
                if hashlib.sha256(source.read_bytes()).hexdigest() != expected:
                    raise ValueError("release hash mismatch")
                if source.suffix == ".py":
                    compile(source.read_text(), rel, "exec")
            rollback = root / "data/releases" / manifest["release_id"]
            if (rollback / "manifest.json").exists():
                raise RuntimeError("release already installed; use a new version or explicit rollback")
            rollback.mkdir(parents=True, exist_ok=True, mode=0o700)
            saved, added = [], []
            try:
                for rel in manifest["files"]:
                    target = root / rel
                    if target.exists():
                        backup = rollback / rel
                        backup.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(target, backup)
                        saved.append(rel)
                    else:
                        added.append(rel)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    temp = target.with_suffix(target.suffix + ".release")
                    shutil.copyfile(stage / rel, temp)
                    os.replace(temp, target)
            except BaseException:
                for rel in saved:
                    shutil.copy2(rollback / rel, root / rel)
                for rel in added:
                    (root / rel).unlink(missing_ok=True)
                raise
            manifest["deployed_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            manifest["rollback_files"] = saved
            manifest["added_files"] = added
            (rollback / "manifest.json").write_text(json.dumps(manifest))
            temporary = root / "data/release.json.tmp"
            temporary.write_text(json.dumps({k: manifest[k] for k in ("release_id", "source_commit", "source_dirty", "deployed_at")}))
            os.replace(temporary, root / "data/release.json")
            print(json.dumps({"installed": manifest["release_id"], "files": len(manifest["files"])}))


def rollback_release(release_id, root):
    root = Path(root).resolve()
    if not release_id.isalnum():
        raise ValueError("invalid release id")
    with (root / "data/portfolio.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        backup = root / "data/releases" / release_id
        manifest = json.loads((backup / "manifest.json").read_text())
        for rel in manifest["rollback_files"]:
            shutil.copy2(backup / rel, root / rel)
        for rel in manifest["added_files"]:
            (root / rel).unlink(missing_ok=True)
        (root / "data/release.json").write_text(json.dumps({"status": "ROLLED_BACK", "from_release": release_id}))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("archive", nargs="?")
    p.add_argument("--root", default=Path(__file__).resolve().parents[1], type=Path)
    p.add_argument("--rollback")
    args = p.parse_args()
    if args.rollback:
        rollback_release(args.rollback, args.root)
    else:
        install(args.archive, args.root)
