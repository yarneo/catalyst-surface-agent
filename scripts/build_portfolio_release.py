"""Build a code-only release. Credentials and mutable state are never inputs."""
from __future__ import annotations
import hashlib
import io
import json
import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULES = "allocations attribution config cycle dashboard_overview event_options_shadow marketdata ownership portfolio_policy research_autopilot runtime_state sessions shadow_regime strategy_evidence portfolio_research".split()
PACKAGES = "backtest broker data learning live notify risk signals social strategies".split()
SCRIPTS = "run_portfolio run_event_options_shadow weekly_research precompute_dashboard revalidate update_allocations retrain check_bil_dividends regime_alert portfolio_lab portfolio_backup portfolio_health portfolio_close install_portfolio_release".split()


def release_files(root=ROOT):
    files = [root / "src/trading_bot" / (name + ".py") for name in MODULES]
    for name in PACKAGES:
        files.extend((root / "src/trading_bot" / name).rglob("*.py"))
    files.extend(root / "scripts" / (name + ".py") for name in SCRIPTS)
    files.extend(p for p in (root / "web").rglob("*") if p.suffix in {".py", ".html", ".css", ".js"})
    files.append(root / "src/trading_bot/options/surface_quality.py")
    return sorted(set(p for p in files if p.is_file()))


def build(destination, root=ROOT):
    paths = release_files(root)
    manifest = {"schema_version": 1, "source_commit": subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(), "files": {}}
    for path in paths:
        rel = path.relative_to(root).as_posix()
        if path.is_symlink() or any(part.startswith(".env") for part in path.parts):
            raise ValueError("unsafe release input")
        manifest["files"][rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest["source_dirty"] = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=root))
    manifest["release_id"] = hashlib.sha256(json.dumps(manifest["files"], sort_keys=True).encode()).hexdigest()[:20]
    with tarfile.open(destination, "w:gz") as archive:
        for path in paths:
            archive.add(path, arcname=path.relative_to(root), recursive=False)
        payload = json.dumps(manifest, indent=2).encode()
        info = tarfile.TarInfo("release-manifest.json")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    Path(destination).chmod(0o600)
    return manifest


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("destination", type=Path)
    args = p.parse_args()
    m = build(args.destination)
    print(json.dumps({"release_id": m["release_id"], "files": len(m["files"])}))
