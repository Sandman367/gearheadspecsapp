# Deploying GearHeadSpecs

Everything is Python standard library — nothing to install beyond Python 3.10
or newer. The whole site is one process serving one SQLite file.

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

On Windows the same with `set HOST=0.0.0.0` then `py app.py`.

## 2. Before anyone else can reach it — change the dev passwords

`data.db` still carries the development accounts, **all with the password
`gearhead`**, including `admin`. Do this first:

```
python3 set_password.py admin
```

Then either set new passwords for the manager accounts you keep
(`cb919_dave`, `m.alvarez`, `SandyVmax`, `c4snakecake`) or suspend the ones you
don't:

```
python3 set_password.py cb919_dave --disable
```

The 26 background `user` accounts (`sohc_sam`, `two_stroke_tina`, …) exist so
votes and flags have people behind them. They can sign in with `gearhead`
until you suspend them; a loop does it:

```
for u in airbox_ali cafe_racer_cy carb_cleaner chainlube_chan clutch_cody dyno_dana \
         fork_seal_fran gp_hayes greasyhands highside_hana kickstart_kim moto_juno \
         nightowl_nadia oilburner_ozzy pannier_pete rekluse_rey rider_kestrel99 \
         sidestand_sue sohc_sam sprocket_sid t.moreno topend_toby torque_tess \
         two_stroke_tina valveshim_vic wrenchmonkey; do
  python3 set_password.py $u --disable
done
```

Suspending keeps their votes and history; it only stops the sign-in.

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
