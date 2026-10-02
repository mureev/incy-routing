# incy-routing

[![ci](https://github.com/mureev/incy-routing/actions/workflows/ci.yml/badge.svg?branch=master&event=push)](https://github.com/mureev/incy-routing/actions/workflows/ci.yml)

[misha-tgshv's Shadowrocket split-routing configs](https://github.com/misha-tgshv/shadowrocket-configuration-file),
rebuilt every day as [INCY](https://github.com/INCY-DEV/incy-platforms) routing profiles and served over HTTP, so
the phone keeps them current by itself through INCY's autorouting. Switching between "Russian services direct"
and "only blocked sites through the tunnel" becomes picking a profile in the app, not turning the VPN off.

The owner directs; Claude Code builds. One instance runs on the owner's server for his household: a personal tool,
not a public service.

```bash
make check      # lint + unit tests, no network
make routing    # build from the committed snapshot and ask real Xray where 72 destinations go
git push        # to master: tested, published as ghcr.io/mureev/incy-routing, live in minutes
```

## The profiles

| File | Name in INCY | Built from | Everything else | Goes direct |
|---|---|---|---|---|
| `ru-direct.json` | RU direct | `sr_ru_geo.conf` + `geosite:category-ru` + misha's bank list + his community list forced to the proxy | tunnel | Russian services by name, then any Russian IP; ~100 bank domains outside the Russian zones; IP checkers; the LAN |
| `blocked-only.json` | Blocked only | `sr_ru_basic.conf`; `geoip:telegram` stands in for its IP-ASN rules | direct | everything not on the lists |
| `sr-geo-literal.json` | SR geo (literal) | `sr_ru_geo.conf` as is | tunnel | Russian IPs (names resolved first), IP checkers, the LAN |

INCY updates a profile in place when its `Name` matches, so these names never change.

**RU direct** is the one to use. In 2026, foreign hosting tends to stall when reached directly from Russia, and
calls need port rules an INCY profile cannot express. "Everything abroad through the tunnel, Russia direct"
sidesteps both. The difference from the literal port: Shadowrocket decides a Russian site by its IP, but under
INCY a name that isn't listed as direct is resolved through the tunnel. A Russian service with foreign edges
(Yandex, VK) can then come back foreign, so RU direct matches Russian services by name first.

## Using it

```bash
docker run -d --name incy-routing -p 127.0.0.1:8080:8080 -v incy-routing:/data \
  --read-only --tmpfs /tmp --cap-drop ALL ghcr.io/mureev/incy-routing:master
```

Put it behind your reverse proxy at a path of your choosing (it serves from `/`), then open, on the phone:

```
incy://autorouting/onadd/https://<your host>/<your path>/ru-direct.json
```

INCY adds the profile, switches it on, and re-fetches it every 24 hours (12 hours to 7 days, in the profile's
settings). A one-off import without auto-update is `incy://routing/onadd/<base64 of the JSON>`; `python -m
incy_routing.convert` writes one as `<slug>.deeplink`.

| Endpoint | |
|---|---|
| `GET /<slug>.json` | a profile; `ETag`, `Last-Modified`, `Cache-Control: no-cache` |
| `GET /status` | build, last attempt and success, `stale`, what is published |
| `GET /healthz` | 200 while the process is up, and nothing more |

Anything else is 404; nothing is listed.

| Environment | Default | |
|---|---|---|
| `INCY_BUILD_AT` | `01:45` | daily build, UTC (04:45 in Moscow, after the lists' 03:00 rebuild) |
| `INCY_JITTER_MIN` | `15` | up to this many minutes added at random |
| `INCY_STALE_HOURS` | `50` | `/status` says `stale` past this |
| `INCY_DATA_DIR` | `/data` | state, cache, geo files, published profiles |
| `INCY_PORT` | `8080` | |
| `INCY_SOURCE` | misha's repo | where the configs and lists come from |
| `INCY_MIRROR` | — | read upstream from `<dir>/<host>/<path>`, offline (tests) |
| `INCY_GEO_REFRESH` | `1` | `0`: never download geo files; they must already be in `<data>/geo` |

## How a build works

1. **Translate.** Every config and the rule lists it pulls in are translated (`incy_routing/convert.py`;
   the rules are in its docstring and [`DECISIONS.md`](DECISIONS.md)).
2. **Check every tag.** Each `geosite:`/`geoip:` tag is checked against
   [Loyalsoldier's geo files](https://github.com/Loyalsoldier/v2ray-rules-dat), the ones INCY bundles. Xray
   refuses to start on an unknown tag, and INCY's own documentation example uses one: `geosite:ru`.
3. **Prune.** Entries a tag already covers are dropped.
4. **Publish all or nothing.**
   - Any list or include that fails to load stops the run.
   - So does a profile that falls below its sanity gate (`incy_routing/profiles.py`).
   - Otherwise changed files are swapped in by atomic rename.
   - When a run is stopped, phones keep the last good profile.
5. **Keep `LastUpdated` unless the rules change.** A newer `LastUpdated` makes INCY re-download about 27 MB of
   geo files.

Not expressible in an INCY profile, and reported in the build log:
- port rules (the voice-call ports);
- `IP-ASN`, `USER-AGENT`, proxy groups;
- MITM modules.

## Development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install --require-hashes --no-deps -r requirements-dev.lock
make check      # 55 tests; the upstream lists are a committed snapshot, the geo files synthetic
make routing    # real Xray v26.9.9 + pinned geo release, checked by SHA-256
make canary     # the same against today's lists and geo data (CI runs it Mondays)
```

`master` is production: see [`DEPLOY.md`](DEPLOY.md). Working on this with an AI agent: [`AGENTS.md`](AGENTS.md).

## Credits

- **Configs and lists:** [misha-tgshv/shadowrocket-configuration-file](https://github.com/misha-tgshv/shadowrocket-configuration-file)
  and the «Про Shadowrocket на русском» community.
- **More lists:** [blackmatrix7/ios_rule_script](https://github.com/blackmatrix7/ios_rule_script) and
  [toneywhitez/zkeen_rules](https://github.com/toneywhitez/zkeen_rules).
- **Geo data:** [Loyalsoldier/v2ray-rules-dat](https://github.com/Loyalsoldier/v2ray-rules-dat).
- **Routing core:** [Xray-core](https://github.com/XTLS/Xray-core).
- **Profile format:** [INCY's documentation](https://github.com/INCY-DEV/incy-docs).

The lists are fetched at run time and served as derived profiles; their content is their authors'.

## Legal

This publishes routing rules built from public lists. It runs no VPN, provides no servers and grants access to
nothing. Whether and how you use a VPN is yours to decide under your local law.

[MIT](LICENSE).
