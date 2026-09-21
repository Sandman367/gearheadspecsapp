# Deploying GearHeadSpecs

Everything is Python standard library — nothing to install beyond Python 3.10
or newer. The whole site is one process serving one SQLite file.

Two ways to run it: **Render** (push to `main`, the site updates; the way it
is set up now) or **your own machine / VPS** (the bundle). Both are below.

## Render: push to main, site updates

The live site is a Render web service built from the GitHub repository
`Sandman367/gearheadspecsapp`, branch `main`. Every push to `main` deploys.
`render.yaml` in the repo is the shape of it:

| Setting | Value | Why |
| --- | --- | --- |
| runtime | Python (`PYTHON_VERSION` 3.13.4) | standard library only, nothing to build |
| start command | `python bootstrap.py` | seeds a database if the disk is empty, then runs `app.py` |
| `HOST` | `0.0.0.0` | Render reaches the process from outside; Render sets `PORT` itself |
| `DATA_DIR` | `/var/data` | the persistent disk — the database and photos live there, so a deploy never touches them |
| disk | `data`, 1 GB, mounted at `/var/data` | the only part not created from this file: add it in the dashboard under *Settings → Disks* (needs the Starter plan) |
| `BOOTSTRAP_ADMIN_PASSWORD` | secret | the admin password of a freshly seeded database; set once, change after first sign-in |

**First boot** on an empty disk: `bootstrap.py` builds a fresh database from
`schema.sql` + `data/` (the same as `py seed.py`), then replaces every seeded
password — admin gets `BOOTSTRAP_ADMIN_PASSWORD`, every other seeded account
gets a random one nobody knows. Nothing can be signed into with the dev
password on a public address.

**Moving the real data up**: sign in as admin → *Admin → Backups & Restore* →
*Restore database from a file…* and pick a `.db` snapshot (the one this was
set up with, or a download from *Download database* on the old machine).
Then *Restore photos from a zip…* with the photos zip. A restore checks the
file (SQLite, passes `integrity_check`, has this app's tables, has an active
admin) before touching anything, then signs everyone out — you sign back in
with the accounts *in the restored file*.

**Back-ups**: *Download database* and *Download photos (zip)* on the same
panel, or `GET /api/admin/backup` with an admin cookie from a script. Keep
them off Render.

**Changing code**: commit, push to `main`, watch the deploy in the Render
dashboard. `py -m unittest test_api` before pushing is the habit that keeps
the live site up.

**Passwords**: everyone has *Password* in the nav (own password, needs the
current one). Admin sets anyone's from *Admin → Members → Set password* and
can *Suspend* / *Reinstate* an account there. `set_password.py` does the same
from a shell where you have one.

## Anywhere else: the bundle

## What's in the bundle

| Path | What it is |
| --- | --- |
| `app.py` | the server: API + static files |
| `data.db` | the database, with every bike, spec, account and message as of the bundle date |
| `data/questionnaire.json` | the question tree (cached at start-up — restart after editing) |
| `data/photos/` | photos managers uploaded (served at `/photos/…`) |
| `static/` | the pages |
| `schema.sql`, `seed.py`, `migrate_*.py` | how the database is built; you do **not** run these on a server that already has `data.db` |
| `set_password.py` | change or suspend an account from the command line |
| `test_api.py` | the test suite (`py -m unittest test_api`, uses a throwaway database) |
| `import_model_lists.py`, `answer_questionnaire.py`, `split_family_bikes.py`, `wire_colors.py`, `fuel_octane.py`, `questionnaire.py` | tooling and modules the server imports |

## 1. Copy it up and run it

```
unzip gearheadspecs.zip -d /srv/gearheadspecs
cd /srv/gearheadspecs
HOST=0.0.0.0 PORT=8420 python3 app.py
```

- `HOST` — `127.0.0.1` by default (local only). Set `0.0.0.0` to listen on
  every interface, or leave it local and put nginx/Caddy in front (recommended;
  that is also where HTTPS goes).
- `PORT` — `8420` by default.
- `DATA_DIR` — unset, the database and photos sit beside the code
  (`data.db`, `data/photos`). Set it to keep them elsewhere, e.g. a mounted
  volume; `bootstrap.py` seeds a database there if none exists.

On Windows the same with `set HOST=0.0.0.0` then `py app.py`.

## 2. Before anyone else can reach it — the dev passwords

A `data.db` built by `seed.py` carries the development accounts, **all with
the password `gearhead`**, including `admin`. A database restored from the
live site does not (their passwords were replaced when the site went up).
If yours does:

```
python3 set_password.py admin
python3 set_password.py cb919_dave --disable      # or a new password
```

or sign in and use *Admin → Members → Set password / Suspend*. The 26
background `user` accounts (`sohc_sam`, `two_stroke_tina`, …) exist so votes
and flags have people behind them; suspending keeps their history and only
stops the sign-in.

## 3. Keep it running

**Linux (systemd)** — `/etc/systemd/system/gearheadspecs.service`:

```
[Unit]
Description=GearHeadSpecs
After=network.target

[Service]
WorkingDirectory=/srv/gearheadspecs
Environment=HOST=127.0.0.1
Environment=PORT=8420
ExecStart=/usr/bin/python3 -u app.py
Restart=on-failure
User=www-data

[Install]
WantedBy=multi-user.target
```

```
sudo systemctl enable --now gearheadspecs
sudo journalctl -u gearheadspecs -f
```

**Windows** — a Scheduled Task, run at start-up, whether or not a user is
logged on: program `py`, arguments `-u app.py`, start in the app folder, with
`HOST` and `PORT` set as environment variables for the task's account.

## 4. HTTPS and a domain

Keep `HOST=127.0.0.1` and proxy. nginx:

```
server {
    server_name specs.example.com;
    location / {
        proxy_pass http://127.0.0.1:8420;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;
    }
}
```

then `certbot --nginx -d specs.example.com`. Caddy does the certificate on
its own: `specs.example.com { reverse_proxy 127.0.0.1:8420 }`.

## 5. Back-ups

Two things hold everything that is not code: `data.db` and `data/photos/`.
The database runs in WAL mode, so **do not copy the file while the server is
running** — a plain copy can miss the last writes. Use SQLite's backup:

```
python3 -c "import sqlite3; s=sqlite3.connect('data.db'); d=sqlite3.connect('backup.db'); s.backup(d)"
```

(or `sqlite3 data.db ".backup backup.db"`). Nightly, kept off the box.

## 6. Updating later

Stop the service, replace the code files (`app.py`, `static/`, `*.py`,
`data/questionnaire.json`), keep `data.db` and `data/photos/`, run any new
`migrate_*.py` the release notes name, start it again. `py -m unittest
test_api` is safe to run on the server at any time: it builds its own
throwaway database and never touches `data.db`.

## 7. Notes

- The server writes nothing outside its folder. File permissions: the
  service account needs write on `data.db`, `data.db-wal`, `data.db-shm` and
  `data/photos/`.
- There is no email. Riders, managers and admin are notified inside the site
  (nav counts, dashboards).
- Registration is open (`/register.html`). If you want it closed for now,
  say so and it becomes an admin-invite flow.
