#!/bin/sh
# Build the profiles, then ask real Xray-core where each test destination in tests/routing/ goes.
#
#   tools/routing_check.sh          the committed upstream snapshot + pinned geo files: deterministic, the CI gate
#   LIVE=1 tools/routing_check.sh   today's upstream + the latest geo files: the weekly canary
#
# The pins move together with the cases: a new geo release or a new Xray is a reviewed change, not a surprise.
# XRAY_VERSION is the core INCY ships (2.6 on iOS, October 2026).
set -eu
XRAY_VERSION=v26.9.9
XRAY_SHA256=1eb9175d0f0a8f8149c9230a7fc5ae66ce332ed20a53155ce61fe62e3f58b7df
GEO_TAG=202610012207
GEOSITE_SHA256=446812233bb3080a82ae3863a0e7f7994bdabcad4a773121111a375a3c903c70
GEOIP_SHA256=391b522361c52804e486a98b53d97f3d9c1d3e4e4217bb34954774a0b456b9fd

cd "$(dirname "$0")/.."
CACHE=${CACHE:-.cache/routing}
XRAY_DIR=$CACHE/xray-$XRAY_VERSION
mkdir -p "$XRAY_DIR"

fetch() {  # url dest sha256 ("-" = don't check)
  if [ ! -s "$2" ]; then curl -fsSL --retry 3 -o "$2.part" "$1" && mv "$2.part" "$2"; fi
  if [ "$3" != "-" ]; then echo "$3  $2" | sha256sum -c --quiet -; fi
}

fetch "https://github.com/XTLS/Xray-core/releases/download/$XRAY_VERSION/Xray-linux-64.zip" "$XRAY_DIR/xray.zip" "$XRAY_SHA256"
[ -x "$XRAY_DIR/xray" ] || python3 -c "import sys, zipfile; zipfile.ZipFile(sys.argv[1]).extract('xray', sys.argv[2])" "$XRAY_DIR/xray.zip" "$XRAY_DIR"
chmod +x "$XRAY_DIR/xray"

DATA=$(mktemp -d)
trap 'rm -rf "$DATA"' EXIT
mkdir -p "$DATA/geo"
if [ "${LIVE:-0}" = 1 ]; then
  LS=https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download
  fetch "$LS/geosite.dat" "$DATA/geo/geosite.dat" -
  fetch "$LS/geoip.dat" "$DATA/geo/geoip.dat" -
  set --
else
  LS=https://github.com/Loyalsoldier/v2ray-rules-dat/releases/download/$GEO_TAG
  fetch "$LS/geosite.dat" "$CACHE/geosite-$GEO_TAG.dat" "$GEOSITE_SHA256"
  fetch "$LS/geoip.dat" "$CACHE/geoip-$GEO_TAG.dat" "$GEOIP_SHA256"
  cp "$CACHE/geosite-$GEO_TAG.dat" "$DATA/geo/geosite.dat"
  cp "$CACHE/geoip-$GEO_TAG.dat" "$DATA/geo/geoip.dat"
  set -- --mirror tests/fixtures/upstream
fi
cp "$DATA/geo/geosite.dat" "$DATA/geo/geoip.dat" "$XRAY_DIR/"

python3 -m incy_routing.build --data-dir "$DATA" --no-geo-refresh "$@"
fail=0
for slug in ru-direct blocked-only sr-geo-literal; do
  echo "== $slug"
  python3 tools/xray_check.py "$DATA/public/$slug.json" "tests/routing/cases-$slug.json" --xray "$XRAY_DIR" || fail=1
done
exit $fail
