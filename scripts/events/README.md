# Event response: wd30 setup

wd30 runs two jobs. One turns NOAA's GOES flare list into orders. The other
checks each station's upload and writes `RECEIPT` or `FAILED`. Both use only
the Python standard library. Michael runs every step below as root on wd30.
Nothing here has been run.

## Layout

    /home/wsprdaemon/hamsci-events/        code: events_server.py, flare_orders.py, stations.json
    /srv/wd_data/events/orders.json        the full orders list (no station can reach it)
    /srv/wd_data/events/incoming/<station>/            that station's rrsync root
        orders/orders.json                 only the orders naming that station
        <event_id>/<station>/hf-timestd/   the station's upload, SHA256SUMS last

The code stays outside every station's rrsync root, so no station key can
rewrite what wsprdaemon runs. Each station key reaches only its own
`incoming/<station>` tree.

For this prototype the recordings sit under `incoming/`. The design's event
tree (`events/<event>/<station>/...`) comes later, as a publish step that
reads from `incoming/`.

## 1. Directories and code

Copy the three files from a dev checkout of hf-timestd. Run these on the
machine that holds the checkout (`CHECKOUT` is its path):

    ssh root@wd30 'install -d -o wsprdaemon -g wsprdaemon /home/wsprdaemon/hamsci-events'
    scp CHECKOUT/scripts/events/events_server.py \
        CHECKOUT/src/hf_timestd/events/flare_orders.py \
        CHECKOUT/scripts/events/stations.json \
        root@wd30:/home/wsprdaemon/hamsci-events/

Then on wd30:

    chown wsprdaemon: /home/wsprdaemon/hamsci-events/*
    chmod 644 /home/wsprdaemon/hamsci-events/*
    install -d -o wsprdaemon -g wsprdaemon /srv/wd_data/events/incoming/AC0G-B4
    chown wsprdaemon: /srv/wd_data/events

`stations.json` lists B4 only. Edit it to add stations, and give each a
`incoming/<id>` directory and its own key line.

After the code is pushed, a clone works too:
`git clone https://github.com/HamSCI/hf-timestd.git`, then copy the same three
files out of it. Until then the scp route is the only one.

## 2. B4's key

Add one line to `~wsprdaemon/.ssh/authorized_keys`, with B4's public key:

    restrict,command="rrsync -no-lock -munge-links /srv/wd_data/events/incoming/AC0G-B4" ssh-ed25519 AAAA... timestd@AC0G-B4

`-no-lock` matters. Without it, rrsync takes a per-directory lock, and one
station's long push blocks every other station. `-munge-links` stops a
station from planting a symlink that points outside its tree.

## 3. Units

    cd CHECKOUT/scripts/events/systemd     # or wherever the unit files sit
    install -m 644 events-orders.service events-orders.timer \
        events-verify.service events-verify.timer /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable --now events-orders.timer events-verify.timer

Both services run as `wsprdaemon` with `Nice=19` and `IOSchedulingClass=idle`.
Orders run hourly. Verify runs five minutes after boot, then every five minutes.

## What the verifier does

* A directory `incoming/<station>/<event>/<station>/<client>/` is ready when
  `SHA256SUMS` exists, its ctime is 60 s old, and no rsync temp file
  (`.name.XXXXXX`) remains. It uses ctime, because `rsync -a` keeps the
  station's mtimes.
* It checks every file `SHA256SUMS` lists, and only those. All match: it writes
  `RECEIPT`, one line, atomically. Otherwise it writes `FAILED`, one bad entry
  per line (a relative path, or `line N` for an unparseable line), and no
  `RECEIPT`.
* `FAILED` also follows from any symlink in the upload, a `SHA256SUMS` that is
  not a regular file or exceeds 1 MiB, an empty list, and any listed path that
  is absolute or holds `..`.
* One bad directory never stops the pass. The verifier logs the error once and
  moves on.
* It never re-verifies a directory that holds `RECEIPT` or `FAILED`.
* It logs a waiting condition once, when it starts or changes. It keeps that
  memory in `incoming/.verify-state.json`.

## Recovering from FAILED

On wd30, remove `FAILED` and the bad files from
`incoming/<station>/<event>/<station>/hf-timestd/`. On the station, the keeper
marks the event `failed:server-verify` and never retries by itself. Edit
`<staging>/state.json`: delete that event's entry (the staging copy stays
valid), then run `hf-timestd events run`. The keeper stages the event again
and pushes it. There is no `events reset` verb yet.

## Checks

    journalctl -u events-verify -u events-orders --since today
    python3 /home/wsprdaemon/hamsci-events/events_server.py verify \
        --root /srv/wd_data/events/incoming     # manual pass, as wsprdaemon
