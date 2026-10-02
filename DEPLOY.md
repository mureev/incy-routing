# Delivery

There is no deploy command. A green push to `master` is the deploy, and nothing in this repository can reach
the server.

## What a push does

1. **CI** (`.github/workflows/ci.yml`) runs three jobs:
   - lint and the unit suite;
   - the routing check against real Xray;
   - an image build for `linux/amd64` that is started offline against the snapshot and must build, serve, and
     report this commit.
2. **`release`** runs only when all three pass on a push to `master`. It publishes that same image, not a rebuild,
   to GHCR as `ghcr.io/mureev/incy-routing:master` and `:sha-<short>`.
3. **The server** polls `:master` every two minutes.
   - A new digest is pulled, started, and checked through the reverse proxy.
   - If the check fails, the previous image is put back.
   - The tool that does this, the public path and the reverse-proxy location live in the owner's infrastructure
     repository, not here.

Other branches and pull requests get the same jobs and publish nothing.

## What the server checks

- **The profile through the proxy:** a new image is kept only if `ru-direct.json`, fetched through the reverse
  proxy, contains `"Name": "RU direct"`.
- **The container's own health:** it must report healthy, which is the image's `HEALTHCHECK` asking `/healthz`.
- **So those two answers are pinned in tests.**
  - Rename that profile, or move its file, and every deploy rolls back while the old image goes on serving.
  - `tests/test_server.py` and `tests/test_build.py` pin both.

## Data

`/data` is a volume, so `LastUpdated` survives a new image and phones don't re-download geo files after a deploy.
Losing it costs one rebuild at start-up and one geo download per phone.

## Rolling back

`git revert` the commit and push. The server deploys the revert like any other commit.
