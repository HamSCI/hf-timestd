# Event response: wd30 setup

wd30 runs two jobs. One turns NOAA's GOES flare list into `orders.json`. The
other checks each station's upload and writes `RECEIPT` or `FAILED`. Both use
only the Python standard library. Michael runs every step below as root on
wd30. Nothing here has been run.

## 1. Checkout and directories

`events_server.py` finds `flare_orders.py` by path. In a checkout it lives at
`src/hf_timestd/events/flare_orders.py`. A bare copy of both files in one
directory works too.

    mkdir -p /srv/wd_data/events/orders /srv/wd_data/events/bin
    chown -R wsprdaemon: /srv/wd_data/events
    sudo -u wsprdaemon git clone https://github.com/HamSCI/hf-timestd.git \
        /srv/wd_data/events/bin/hf-timestd

The orders unit reads `scripts/events/stations.json` from that checkout. The
file lists B4 only. Edit it there to add stations.

## 2. B4's key

Add one line to `~wsprdaemon/.ssh/authorized_keys`, with B4's public key:

    restrict,command="rrsync -no-lock /srv/wd_data/events" ssh-ed25519 AAAA... timestd@AC0G-B4

`-no-lock` matters. Without it, rrsync takes a per-directory lock, and one
station's long push blocks every other station.

## 3. Units

    cd /srv/wd_data/events/bin/hf-timestd/scripts/events/systemd
    install -m 644 events-orders.service events-orders.timer \
        events-verify.service events-verify.timer /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable --now events-orders.timer events-verify.timer

Both services run as `wsprdaemon` with `Nice=19` and `IOSchedulingClass=idle`.
Orders run hourly. Verify runs five minutes after boot, then every five minutes.

## What the verifier does

* A directory `<event>/<station>/<client>/` is ready when `SHA256SUMS` exists,
  its ctime is 60 s old, and no rsync temp file (`.name.XXXXXX`) remains. It
  uses ctime, because `rsync -a` keeps the station's mtimes.
* It checks every file `SHA256SUMS` lists, and only those. All match: it writes
  `RECEIPT`, one line, atomically. Any file missing or different: it writes
  `FAILED`, one bad path per line, and no `RECEIPT`. An absolute path or a `..`
  in `SHA256SUMS` also gives `FAILED`.
* It never re-verifies a directory that holds `RECEIPT` or `FAILED`. To retry a
  failed upload, remove `FAILED`. The station keeps its copy until `RECEIPT`
  arrives.
* It logs a waiting condition once, when it starts or changes, not on every pass.
  It keeps that memory in `<root>/.verify-state.json`.

## Checks

    journalctl -u events-verify -u events-orders --since today
    python3 events_server.py verify --root /srv/wd_data/events   # manual pass
