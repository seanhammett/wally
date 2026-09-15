#!/usr/bin/env bash
# Publish the built site/ to the gh-pages branch of origin, for GitHub Pages.
#
# The branch is a single orphan commit that is force-pushed every time, so the
# repository does not grow by the size of the tiles on each deploy. main never
# carries site/tiles or site/stats; they only ever live on gh-pages.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
site="$root/site"
remote="$(git -C "$root" remote get-url origin)"

if [ ! -f "$site/layers.json" ]; then
  echo "site/ has not been built — run 'make build' first." >&2
  exit 1
fi

# GitHub refuses any file of 100 MiB or more; catch it here rather than after
# uploading everything else.
big="$(find "$site" -type f -size +99M)"
if [ -n "$big" ]; then
  echo "GitHub rejects files over 100 MiB:" >&2
  ls -lh $big >&2
  exit 1
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

cp -R "$site/." "$tmp/"
find "$tmp" -name .DS_Store -delete
touch "$tmp/.nojekyll"   # serve files as-is; skip the Jekyll build

rev="$(git -C "$root" rev-parse --short HEAD)"
# site/ is deployed as it is on disk, so say when that is not what HEAD holds.
git -C "$root" diff --quiet HEAD -- . || rev="$rev+uncommitted"
git -C "$tmp" init -q -b gh-pages
git -C "$tmp" add -A
git -C "$tmp" commit -q -m "Deploy site from $rev"
git -C "$tmp" push -f "$remote" gh-pages

echo "Pushed gh-pages. First time: enable Settings → Pages → Deploy from a branch → gh-pages / (root)."
