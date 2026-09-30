#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# tiktok_dl.sh  —  Lanzador TikTok Bulk Downloader
# Corre en la terminal actual para tener trazabilidad completa.
# ─────────────────────────────────────────────────────────────────────────────

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$DIR/tiktok_dl.py"
OUTDIR="${1:-$HOME/Downloads/TikTok}"

echo "🔄 Actualizando yt-dlp..."
yt-dlp -U 2>/dev/null | grep -E "Updated|up to date|Ya está" || true
echo ""

exec python3 "$SCRIPT" --out "$OUTDIR" --workers 8
