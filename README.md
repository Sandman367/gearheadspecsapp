# GearHeadSpecs

A community-maintained motorcycle spec database. Riders look up the real
numbers for their machine; the person closest to each bike owns its accuracy.

Built from four design mockups plus the Aug 2026 identity schema. Everything
that was hardcoded JavaScript in those files is now a real database.

Pure Python standard library — no `pip install` needed.

## Run it

```
py seed.py     # build data.db from schema.sql + data/
py app.py      # serve on http://127.0.0.1:8420/
```

Then sign in at <http://127.0.0.1:8420/login.html>. Every seeded account uses
the password `gearhead` — **dev only, change before this is reachable by
anyone else.**

| Account | Role | What it shows |
| --- | --- | --- |
| `admin` | admin | Every queue: proposals, tree flags, user flags, divergence |
| `m.alvarez` | manager | Manages the CB919, owns its open flag queue |
| `cb919_dave` | user | Has a bike in the garage with service history |
| `rider_kestrel99` | user | Submitted one of the open flags |
| `t.moreno` | user | Empty garage — the first-run experience |

Plus 24 background community accounts (all role `user`), because a vote is a row keyed to a
user: a link cannot show 22 votes unless 22 people exist to have cast them.

## Test

```
py test_api.py
```

125 end-to-end tests against a throwaway database. They cover the role gates,
the flag lifecycle, vote toggling, questionnaire branching, garage privacy,
and path traversal.

## The pages

| Page | Who | What |
| --- | --- | --- |
| `index.html` | anyone | Search, spec sheet, garage, service log |
| `manager.html` | manager | Flag queue, resolved log, not-sure answers, proposals |
| `enrich.html` | anyone | Tools and guides attached to one spec |
| `questionnaire.html` | admin / manager | Admin adds a bike and builds its tree; manager fills in the values |
| `admin.html` | admin | Manager assignment, proposals, tree flags, user flags, divergence, provenance |
| `login.html` | anyone | Sign in |

### Who is allowed to do what

The split that everything else follows: **admin owns the tree, the manager owns
the values.**

There are three stored roles — `user`, `manager`, `admin` — and one state that
is deliberately *not* a role: **public**, meaning nobody is signed in.

| | admin | manager | user | public (not signed in) |
| --- | --- | --- | --- | --- |
| Read every spec, alternate and vote count | ✅ | ✅ | ✅ | ✅ |
| Read tools and guides | ✅ | ✅ | ✅ | ✅ |
| Vote on any value, flag any value | ✅ | ✅ | ✅ | ❌ |
| Request a gap be filled | ✅ | ✅ | ✅ | ❌ |
| Add tools/links | ✅ | ✅ | ✅ | ❌ |
| Fill an empty spec value | ✅ | ✅ | ✅ | ❌ |
| Keep a garage and service log | ✅ | ✅ | ✅ | ❌ |
| Fix or dismiss a flag | ✅ | assigned bikes only | ❌ | ❌ |
| Confirm a community value | ✅ | assigned bikes only | ❌ | ❌ |
| Propose a new field | ✅ | ✅ | ❌ | ❌ |
| Add a bike / build its spec tree | ✅ | ❌ | ❌ | ❌ |
| Assign a manager to a bike | ✅ | ❌ | ❌ | ❌ |
| Approve a new field | ✅ | ❌ | ❌ | ❌ |

**Public is the absence of a session, not a row in `users`.** That is why the
signed-in role is called `user` and not `public`: a stored role by that name
would make every permission check ambiguous — does `role='public'` mean "signed
in with no privileges" or "not signed in at all"? Now it can only mean one
thing. `migrate_role_rename.py` performs that rename on an existing database.

An anonymous visitor sees the site fully populated and **every control stays
visible and clickable**. Hiding them would mean a first-time reader never
learns the site is editable. Clicking one opens an inline panel explaining what
it would do, with a Sign in link — and the click is stopped in the capture
phase, before any page handler sees it, so no page has to remember to re-check.
The server refuses the write regardless; the panel is courtesy, not security.

**A manager never chooses their own bikes — an admin assigns them.** That is
what makes "the person closest to the bike" mean anything, and it is why a
value flag can be routed to one named person instead of a shared pile. Bike
creation is therefore admin-only too: letting a manager create a bike would be
self-assignment through the back door.

Assignment lives in the admin console's **Bike Managers** panel — pick an
unassigned bike and a person from two dropdowns. Assigning promotes a `user`
account to `manager`; removing their last bike sets it back, so nobody keeps
manager access to nothing.

## How it is put together

```
app.py             HTTP server: ~56 REST routes, session auth, static files
schema.sql         The whole database, with the reasoning in comments
seed.py            Builds data.db from data/
questionnaire.py   The branching engine, shared by seed.py and app.py
data/              Seed data extracted from the mockups
static/            The six pages, app.css, api.js
archive/mockups/   The original design files, kept for provenance
```

### A bike is an id, not a name

Carried forward from `bike_identity_4.sql`. `bikes` holds one spec set;
`bike_names` holds however many names it goes by; `bike_years` holds the model
years it covers. "CB900F2 919", "2005 Honda CB919" and "Hornet 900" are three
names for one machine, so searching any of them lands on the same page.

Specs attach to the **bike**, never to a year. When a year genuinely differs,
the bike is split and the years divide between them — there is no inheritance
and no override. A split copies specs, so `sibling_divergence` and the admin
divergence queue exist to make later drift visible instead of silent.

### The catalog was folded, not flattened

The source catalog is 991 flat `(Model, Year)` rows. `seed.py` folds them into
258 bikes: within a model, a *contiguous* run of years whose spec values are
all identical becomes one bike. Runs must be contiguous — otherwise a bike's
displayed year range would span years it does not own.

Every folded bike gets `years_verified = 0`. The rows carried no model-year
boundaries, so those spans are inferences, and the admin console lists them as
unconfirmed until someone checks.

### Catalog provenance is kept, not decided

The original catalog has 991 model-year rows; the v2 browse page carried 139.
Rather than pick one, both are imported and `bike_years.in_v2` records which
survived. The 847 dropped rows are a review list in the admin console, so
narrowing the catalog stays a decision someone makes rather than an accident of
which file got loaded.

### A gap is not a guess

`specs.value IS NULL` means "this field applies to this bike, nobody has
sourced it yet". It renders as *Not yet sourced*, never as a plausible-looking
number. The questionnaire's "Not 100% sure" answer routes a question to admin
and **leaves the gap open** — closing it would trade an honest blank for a
false answer.

### Votes are rows

The mockups stored vote counts as integers on an object, so nothing stopped one
person voting twice and a withdrawn vote had nowhere to go. `alternate_votes`,
`spec_votes` and `link_votes` are keyed `(item, user)`; counts come from views.
One vote per person, withdrawable, correct everywhere at once.

The **stock value** is votable too, not just competing alternates — a rider
saying "that part number is right, it fitted mine" is the most useful signal on
the page, and it previously had nowhere to go.

### Requesting a gap

`spec_requests` is the demand signal. A manager facing forty empty fields has
nothing to say which ones anybody needs, so the order of work is guesswork; the
same forty ranked by "6 riders asked for this" is a to-do list. It only applies
to a gap — once a value exists the request is answered.

### The manager's "Fixed spec" tag

A bike's manager can tag one spec on their bike as **Fixed spec** — one correct
value, which they set. While tagged, nobody else can submit a value or an
alternate for it. Flagging stays open, because disagreeing is not contributing.

The tag is stored on the `specs` row, not `spec_fields`. `spec_type` on the
field is platform-wide, so a manager toggling it there would change that field
on all 260 bikes — admin's authority, not theirs. `specs.spec_type` overrides
it for one bike; NULL means inherit, which is also how the manager clears the
tag rather than leaving it stuck at their last choice.

**The lock honours the manager's explicit tag, not the inherited default.**
2,927 of 2,932 spec rows are `fixed` by inheritance, 1,950 of them empty, so
treating the default as a lock would have closed almost every gap in the
database and cancelled "anyone signed in can fill a gap". The inherited default
still does its own job — it refuses alternates. Closing a spec to *values* is a
deliberate act on one bike.

### Who did what

`admin_actions` records every admin mutation with a summary already written for
a human — approvals, branch changes, manager assignments, suspensions,
deletions. The console's **My Activity** panel shows your own by default, with
a toggle to widen it and a filter for the destructive ones.

The row is written on the same connection as the change, so it commits or rolls
back with it: a refused action never appears as though it happened.

`detail` earns its place on the destructive entries. Deleting a field records
the values it destroyed, bike by bike, because after the cascade that log entry
is the only remaining record they existed. It is not undo and does not pretend
to be — it is the difference between "we can find out what was lost" and "it is
gone".

### Removing a branch or a spec

The console has a searchable **Manage the Spec Tree** panel — 113 fields is too
many to list, so it is search-first. Each row carries the two numbers that
decide whether removing something is safe: how many bikes hold the field, and
how many of those hold a value somebody sourced.

Three removals, in increasing order of damage:

- **Remove a branch** — the field stops reaching *future* bikes. Bikes that
  already have it keep it, values and all.
- **Remove from a bike** — one field off one bike. Refused while that spec holds
  a value or alternates unless explicitly confirmed: "this field does not belong
  on this bike" is a different statement from "throw away what people recorded".
- **Delete the field** — refused outright while any bike uses it, unless
  confirmed. Deleting cascades the spec rows away and takes their values,
  alternates and votes with them, so the error names the counts
  ("3 bike(s) already use this field, 1 with a value") and the button restates
  them before a second click will fire.

### An approved field has to land somewhere

Approving a branch proposal used to create a row in `spec_fields` and stop
there. `build_spec_tree` walks the static `data/questionnaire.json`, so a field
created at runtime could never be triggered by any bike built afterwards — it
existed, appeared in no tree, and did nothing. The only way it reached a bike
was a tick-box that put it on one bike, once.

Two tables close that:

- **`field_triggers`** — "this field applies when Q3 is answered A".
  `questionnaire.json` still defines the built-in tree; this is how fields
  added later join the same branches, and `build_spec_tree` consults both.
- **`bike_answers`** — what a bike actually answered. It is the only record of
  *why* a bike has the fields it has, and a field attached to a branch later
  needs it to find the bikes it should apply to.

**Approving does not guarantee the field appears anywhere**, and that caught
us out in practice: three real approvals created fields that reached no bike,
the dialog said "Field created", and nothing surfaced them again. Two things
now prevent a silent orphan — the dialog warns while the choice can still be
changed, and the admin console has a **Fields on no bike** panel that lists
them with a bike search to place them (or a delete, refused once any bike uses
the field).

That panel distinguishes two cases that look identical in the data. A field
triggerable by `questionnaire.json` but used by no bike is *healthy* — belt
drive and 2-stroke fields simply await a bike that answers that way, and 41 of
them do. A field with no trigger at all reaches nothing and never will. Only
the second kind is listed; the first is a footnote.

A field reaches bikes one of three ways, and the approve dialog asks which:

- **Every bike** (`spec_fields.universal`) — for specs no questionnaire answer
  controls, like road trip tools. A flag rather than 260 trigger rows, so a bike
  created next year is covered without anyone remembering to re-run anything.
  Un-marking it stops future bikes receiving it but never deletes existing spec
  rows, which may hold values somebody entered.
- **One or more questionnaire branches** — a field may belong to several, and
  they are a UNION rather than a contradiction: a chain/belt tension spec sits
  on `q3=A` and `q3=B` while staying off `q3=C`. The dialog adds them one at a
  time as removable chips, and back-filling counts a bike matching two of them
  once, not twice.
- **Named bikes** — the only route that reaches bulk-catalog bikes.

The approve dialog also lets the admin **reword the field before it is
created**, pre-filled with what was proposed. A proposer types "belt
conditioner" in a hurry; the label goes on the tree and is read by every rider
on every bike, so the wording is settled once, here. The proposal row keeps the
original text, so the change stays visible rather than rewriting history.

The approve dialog therefore asks where the field lives: a questionnaire branch
(showing how many bikes already answered that way, with back-filling as an
explicit choice rather than a silent side effect), a named bike, or both.

Bulk-catalog bikes have no `bike_answers` rows, so no branch reaches them. That
is deliberate — treating a spreadsheet row that happens to say "Chain and
sprockets" as an answer to Q3 would be inventing an answer nobody gave. Fields
reach those bikes by being added directly.

### A flag names which value it means

`value_flags.alternate_id` distinguishes a flag against the stock value from
one against somebody's alternate. They share a table so the manager's queue
stays one query, but the queue shows which, and **Fix Value is refused on an
alternate flag** — writing `new_value` there would overwrite the manual's value
with an edit aimed at a competing suggestion.

### Service intervals are read, not copied

A service task points at the spec that states its interval
(`service_tasks.interval_field_key`). Correct the oil-change interval on the
spec page and every rider's next-due mileage moves with it — the task table
never holds a stale copy. There is a test for exactly this.

## Things fixed along the way

Changes made deliberately while porting, each a bug in the source material:

1. **The overlap constraint did not constrain.** `bike_identity_4.sql` says "a
   given model-year belongs to exactly one bike" but implemented it as
   `UNIQUE (bike_id, year)`, which only stops one bike listing 2006 twice — two
   sibling bikes could both claim 2006, the case that actually matters after a
   split. The real constraint spans `bikes` and `bike_years`, so it is a trigger
   now.
2. **`UNIQUE` columns that were nullable.** SQLite treats NULLs as distinct, so
   `UNIQUE (bike_id, name, market)` with a null market permitted unlimited
   duplicates. `market` is `NOT NULL DEFAULT ''`.
3. **One field, two categories.** The CB919 sheet filed "Oil Filter Part Number"
   under Engine; the questionnaire filed it under General. Canonicalised to
   Engine. `seed.py` raises on any such collision rather than silently merging.
4. **"Ignition Order" was registered under both Engine and Electrical**, which
   would have produced two fields with one name.
5. **Near-duplicate field names.** The wizard said "Intake Valve Clearance", the
   spec sheet said "Valve Clearance — Intake". An alias map resolves them to one
   field, so a bike cannot show both with one permanently blank.
6. **Branching was client-side JavaScript.** The questionnaire's `next()`
   closures could not be trusted from a browser — a hand-written POST could
   claim any fields it liked. Branching is declarative data now, and the server
   re-walks it on submit.
7. **`Access-Control-Allow-Origin: *` alongside cookie auth** — removed; the
   pages are same-origin.
8. **Static serving could climb out of its directory.** Now normalised and
   checked, with a test.
9. **`innerHTML` everywhere.** Fine when every string was a hardcoded literal;
   stored XSS once values come from other users. All interpolation goes through
   `esc()`, and link URLs through `safeUrl()`.

## Known gaps

- **No CSRF tokens.** Session cookies are `SameSite=Lax`, which blocks
  cross-site form POSTs, and the API only accepts JSON bodies. That is adequate
  for local use but is not the same as real CSRF protection — add tokens before
  this faces the internet.
- **Passwords are pbkdf2-sha256 at 200k rounds**, which is reasonable, but
  there is no rate limiting on `/api/auth/login`.
- **Single-process SQLite.** Fine for one machine; needs Postgres and a real
  WSGI server to go multi-user over a network.
- **Only Honda.** The schema has a `make` column and no Honda assumption, but
  the seed catalog is Honda-only.
- **`files.zip` / `files2.zip`** in the repo root are untouched — I did not know
  what they were for.
