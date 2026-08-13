# Bike Spec Platform — Integration Backend

Real SQLite-backed REST API implementing the full schema designed across
this project. Pure Python standard library — no pip installs required.

## Run it

    python3 app.py

Serves the API on http://localhost:8420/api/* and the static HTML files
(copies of the dashboards) on http://localhost:8420/

## Re-seed the database

    python3 seed.py

Wipes and rebuilds data.db from schema.sql with the example data
(CB919, CR125, XR650L, sample flags/proposals/managers) used throughout
the design conversation.

## Test the API without a browser

    python3 test_api.py    # core endpoint coverage
    python3 test_ids.py    # ID-based route coverage (see below)

Both spin the server up in-process, exercise the endpoints, and print results.

## Resolved since the first pass

1. **Username-as-ID fragility** — usernames like "M. Alvarez" contain
   spaces/periods that are fragile as URL path segments (needed explicit
   URL-decoding server-side to even work). Added stable numeric-ID routes
   as the recommended path going forward, alongside the original
   username-based routes for backward compatibility:
   - `GET /api/users` — list every user with their stable numeric id
   - `GET /api/users/<id>/profile` — ID-based profile lookup
   - `GET /api/bike-managers/<id>/flags` — ID-based manager flag queue
   Both verified to return identical results to their username-based
   equivalents (see test_ids.py).

2. **Orphaned prototype file** — the earliest CB919/CR125 build
   (spec-page-prototype.html) was superseded entirely by honda-browser.html
   partway through the project but never formally retired. Moved to
   /archive/spec-page-prototype_RETIRED.html so it's no longer a second,
   disconnected "spec page" implementation living alongside the real one.

## Known gap — not yet done

The four HTML dashboard files in /static are still using their original
embedded static/mock data — they are NOT yet wired to call this API.
That's the next phase: replacing their hardcoded JS arrays with fetch()
calls to these endpoints. honda-browser.html in particular has a large
embedded ~991-row Honda catalog as static JS; only the 3 fully-built
bikes (CB919/CR125/XR650L) exist in the real database so far.
