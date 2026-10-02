# Decisions

Why things are the way they are. Each entry says what was decided, why, and what would change it.

## 1. Shadowrocket rules map onto INCY's buckets with little loss

**Decision.** INCY's profile has three buckets (direct, proxy, block) and `GlobalProxy` for everything else.
Shadowrocket's ordered rules become:

| Shadowrocket | INCY |
|---|---|
| `DOMAIN-SUFFIX` | `domain:` |
| `DOMAIN` | `full:` |
| `DOMAIN-KEYWORD` | `keyword:` |
| `DOMAIN-WILDCARD` | `regexp:` |
| `IP-CIDR` | the CIDR |
| `GEOIP` | `geoip:` |
| `FINAL` | `GlobalProxy` |

- **Always prefixed.** A bare string in an Xray domain list is a substring match, not a domain, so every entry
  carries its kind.
- **Cyrillic becomes punycode.** Xray matches the punycode name: `*.рф` is `domain:xn--p1ai`.

**Why.** It is what INCY consumes. It was verified against real Xray: the routing cases and `tools/xray_check.py`.

**What would change it.** INCY gaining port or process rules in profiles. Then the dropped `DST-PORT` voice
rules could come back.

## 2. `skip-proxy` is not a routing rule

**Decision.** `skip-proxy` names (`*.ru`, `*.su`, `*.рф` in `sr_ru_geo`) are reported, not routed.
`--skip-proxy-direct` gives the other reading.

**Why.** On iOS, `skip-proxy` moves those connections from the app's HTTP-proxy layer to its TUN, where the rules
still decide (Surge manual, "Misc Options"; misha's own comment in the config says the same). In Shadowrocket a
`.ru` site goes direct because its IP is Russian, and a blocked `.ru` site hosted abroad goes through the tunnel.
`sr_nonru_basic` only makes sense under this reading: it lists `*.ru` in skip-proxy *and* sends `.ru` to the
proxy.

**What would change it.** Shadowrocket documentation saying otherwise.

## 3. RU direct matches Russia by name, not only by IP

**Decision.** RU direct adds `geosite:category-ru`, misha's bank list (the ~100 entries outside the Russian zones
survive pruning), and his community list forced to the proxy.

**Why.** A name INCY does not list as direct is resolved by its remote DNS, through the tunnel. A Russian service
with foreign edges (Yandex, VK) can then come back with a foreign IP and get tunnelled, which is exactly what a
bank app objects to. Matching by name first makes the DNS answer irrelevant.

**The price.** A blocked `.ru` site hosted abroad also matches by name and goes direct, unless it is on the proxy
list and the client honours `RouteOrder` (§4). The literal port keeps Shadowrocket's behaviour for anyone who
prefers it.

**What would change it.** Bank apps that stop caring about the IP, or a curated "blocked .ru" list small enough to
force to the proxy.

## 4. `RouteOrder: block-proxy-direct`, set from the config's first policy

**Decision.** `RouteOrder` comes from the first non-block policy in the config's `[Rule]` order.

**Why.** Shadowrocket is first-match; INCY is buckets. They disagree only where a proxy entry sits inside a direct
one: three sites in RU direct (seasonvar.ru, grani.ru, novayagazeta.ru). Happ documents `RouteOrder`, and
RoscomVPN and PinRouting ship it in their INCY profiles. INCY's own documentation does not mention it.

**What would change it.** INCY documenting a different field, or a phone test showing it is ignored. Then those
three go direct, as they would in Shadowrocket if their IPs were Russian.

## 5. `DomainStrategy` follows `no-resolve`

**Decision.** `IPIfNonMatch` if any IP rule in the config resolves names (no `no-resolve`), else `AsIs`.

**Why.** That is Shadowrocket's semantics. Inclusion profiles resolving every unlisted name would add a DNS round
trip to most connections for nothing.

## 6. "Everything else" is the default outbound, never a catch-all rule

**Decision.** `tools/xray_check.py` compiles `GlobalProxy` as Xray's default outbound (the first one).

**Why.** `IPIfNonMatch` re-matches by IP only when *no* rule matched. A trailing `network: tcp,udp` rule matches
in the first pass, so `geoip:ru` never fires for names. The first version of the harness did exactly that, copied
from 3x-ui's JSON-subscription code, and sent a Russian-hosted test site into the tunnel.

**What would change it.** Nothing; it is how Xray works. It matters for anyone writing full Xray configs.

## 7. Publish all or nothing, and keep `LastUpdated` stable

**Decision.** `--strict`, a sanity gate per profile, atomic renames, and only changed files. `LastUpdated` moves
only when the rules change.

**Why.** Phones re-fetch daily. A half-built profile, built from a list that 404s or was emptied, would route a
household wrongly until the next good build. Keeping the last good one costs nothing. A newer `LastUpdated` makes
INCY re-download ~27 MB of geo files, because Loyalsoldier publishes `.sha256sum` and not the bare `.sha256` INCY's
skip check wants.

## 8. Standard library only, Loyalsoldier for validation only

**Decision.** No runtime dependencies. The stdlib IDNA 2003 codec is enough for the Cyrillic names in the lists
(checked against IDNA 2008 on every entry). The geo files only validate and prune; phones use whatever INCY
downloads from the profile's `Geositeurl`.

**What would change it.** A list with names where IDNA 2003 and 2008 disagree (ß, ς, joiners). Then add `idna`.

## 9. Liveness and freshness are separate

**Decision.** `/healthz` says only that the process is up. Staleness is in `/status`.

**Why.** An upstream outage is not something a restart fixes, and the last good profiles keep being served
meanwhile. A health check that failed on staleness would roll back good deploys during an outage.
