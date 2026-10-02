"""The profiles this service builds, from misha-tgshv's Shadowrocket configs.

`name` is what INCY shows, and what it matches on: importing a profile whose Name already exists updates it in
place. Never rename one that is in use; add a new one instead.

`expect` is the sanity gate. A build whose output falls below it is not published, so an upstream list that
is emptied, renamed or replaced by an error page can never reach a phone.
"""

from __future__ import annotations

SOURCE = "https://raw.githubusercontent.com/misha-tgshv/shadowrocket-configuration-file/main"

PROFILES = [
    {
        # Exclusion, the recommended default: everything through the tunnel except Russia (by name, then by
        # IP) and the banks. misha's community list is forced to the proxy so a blocked .ru name in it still
        # gets through, provided the client honours RouteOrder.
        "slug": "ru-direct",
        "name": "RU direct",
        "conf": "{source}/conf/sr_ru_geo.conf",
        "args": [
            "--add-direct-site", "geosite:category-ru",
            "--add-direct-list", "{source}/rules/domains_banking.list",
            "--add-proxy-list", "{source}/rules/domains_community.list",
        ],
        "expect": {"GlobalProxy": "true", "min": {"DirectSites": 50, "ProxySites": 100},
                   "must": ["geosite:category-ru", "geoip:ru"]},
    },
    {
        # Inclusion: only the curated blocked list through the tunnel. geoip:telegram stands in for the
        # Telegram list's IP-ASN rules, which have no Xray equivalent.
        "slug": "blocked-only",
        "name": "Blocked only",
        "conf": "{source}/conf/sr_ru_basic.conf",
        "args": ["--add-proxy-ip", "geoip:telegram"],
        "expect": {"GlobalProxy": "false", "min": {"ProxySites": 100, "ProxyIp": 50},
                   "must": ["geoip:telegram"]},
    },
    {
        # sr_ru_geo.conf with nothing added: Russian sites decided by their IP, exactly as in Shadowrocket.
        "slug": "sr-geo-literal",
        "name": "SR geo (literal)",
        "conf": "{source}/conf/sr_ru_geo.conf",
        "args": [],
        "expect": {"GlobalProxy": "true", "min": {"ProxySites": 1}, "must": ["geoip:ru"]},
    },
]
