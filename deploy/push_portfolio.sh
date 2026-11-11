#!/usr/bin/env bash
# Code-only portfolio deployment. Host names and credentials stay in the caller's environment.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
: "${VM:?Set the private deployment VM name}"
: "${ZONE:?Set the VM zone}"
: "${REMOTE_ROOT:?Set the private absolute runtime directory}"
[[ "$VM" =~ ^[a-zA-Z0-9-]+$ && "$ZONE" =~ ^[a-zA-Z0-9-]+$ && "$REMOTE_ROOT" =~ ^/[a-zA-Z0-9/_-]+$ ]] || exit 2
PACKAGE="$(mktemp -t portfolio-release.XXXXXX)"
trap 'rm -f "$PACKAGE"' EXIT
cd "$ROOT"
uv run --extra portfolio python scripts/check_no_secrets.py --no-history
uv run --extra portfolio python scripts/build_portfolio_release.py "$PACKAGE"
gcloud compute scp --quiet --zone="$ZONE" "$PACKAGE" "$VM:/tmp/portfolio-release.tar.gz"
gcloud compute scp --quiet --zone="$ZONE" scripts/install_portfolio_release.py "$VM:/tmp/install_portfolio_release.py"
gcloud compute ssh "$VM" --quiet --zone="$ZONE" --command="cd '$REMOTE_ROOT' && .venv/bin/python /tmp/install_portfolio_release.py /tmp/portfolio-release.tar.gz --root '$REMOTE_ROOT' && .venv/bin/python scripts/run_portfolio.py --phase open --inspect --no-enrich && sudo systemctl restart trading-web"
