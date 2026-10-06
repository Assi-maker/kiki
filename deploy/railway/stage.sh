#!/usr/bin/env sh
# Build an upload directory for one Railway service:
#   ./stage.sh trend|polymarket <out_dir> [seed_db]
# seed_db (optional) is copied into the volume once on first start; then:
#   cd <out_dir> && railway up --service <name> --ci
set -e
svc="$1"; out="$2"; seed="$3"
here="$(cd "$(dirname "$0")" && pwd)"; root="$here/../.."
rm -rf "$out"; mkdir -p "$out/seed"
cp "$here/Dockerfile" "$out/"
cp "$here/start-$svc.sh" "$out/start.sh"
cp -r "$root/crypto_trading" "$out/"
find "$out" -name __pycache__ -type d -prune -exec rm -rf {} +
if [ -n "$seed" ]; then cp "$seed" "$out/seed/"; else touch "$out/seed/.empty"; fi
