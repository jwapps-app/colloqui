# Design principles

How this codebase is organised, and the rules that keep each change small.
Read this before adding a feature. The reasoning behind each rule is in
`docs/design-review-2026-11-25.md`.

## Where things live

### Server (`server/app`)

| Module | Owns |
|---|---|
| `routes/*.py` | HTTP only. Parse the request, check access, call one function, return a schema. |
| `access.py` | Every permission rule: who is a member, a channel manager, a space manager. |
| `membership.py` | Who belongs to what, and the rules that keep an owner or manager in place. |
| `messaging.py` | How a message is serialized, and everything that follows when one is posted or changed. |
| `events.py` | Telling clients and integrations that something changed. |
| `db.py` | The session, the `DB` dependency, and the after commit queue. |
| `vocab.py` | Every closed set of values: roles, notify levels, recurrences, reactions. |
| `tasks.py` | The task line grammar (`[ ] ` and `[x] `). |
| `storage.py` | Every path under the upload directory. |
| `assets.py` | The web client's build id. |
| `notify.py` | Creating and delivering a notification. The only caller of the push transports. |
| `background.py` | Fire and forget work off the request path. |

### Web client (`server/app/static`)

| File | Owns |
|---|---|
| `app.js` | Wiring only: connects the page's controls to the modules, then starts the app. |
| `js/state.js` | The state every module shares. |
| `js/dom.js`, `js/format.js` | Helpers with no app state. |
| `js/markdown.js`, `js/tasks.js` | Message rendering and the task grammar. No network, so they run under Node. |
| `js/rows.js` | How one item looks in a list. Each kind is defined once. |
| `js/meta.js` | The server's vocabulary, read from `/api/v1/meta`. |
| other `js/*.js` | One feature each: channels, messages, threads, panes, realtime, settings, admin. |

## Rules

### Server

1. **Route handlers stay thin.** A rule that two handlers need goes in a module under `app/`. A route module never imports from another route module.
2. **Get the session with `db: AsyncSession = DB`.** Never write `Depends(get_db)`. `DB` commits before the response is sent, so a client can act on a response at once.
3. **Never tell the outside world about a change inline.** Use `events.publish`, `events.emit` or `db.after_commit`. They run only after the commit succeeds. Calling `manager.send_to_users` from a handler is wrong. The one exception is purely transient signals in `ws.py` (typing, presence).
4. **When a handler must commit early, call `commit(db)`,** not `db.commit()`, so queued work is delivered.
5. **A message is serialized only by `messaging.serialize_messages`,** with one of the named `Part` choices. Never build a `MessageOut` by hand. A deleted message always comes out as a tombstone.
6. **A message is posted only through `messaging.post_message`** and changed only through the `publish_*` functions. Which side effects each change has is decided there.
7. **Every permission check goes through `access.py`.** Look for the existing function before writing a check.
8. **A value the database, the server and the client must agree on is defined once in `vocab.py`.** Schemas use the enums. The models carry a CHECK constraint built from them. The client reads them from `/meta`.
9. **Paths under the upload directory come from `storage.py`.** Delete a blob with `after_commit(db, storage.unlink_blobs, ids)`, never before the row's deletion commits.
10. **One way to build a thing.** If a list form and a single form exist, the single form calls the list form (`channel_out` calls `channels_out_bulk`).
11. **A schema or data change is an Alembic migration** that is idempotent and cannot fail a deploy. Startup does no data work.

### Web client

12. **`app.js` holds wiring only.** New behaviour goes in a module under `js/`.
13. **Never write a version number.** Reference assets as `/assets/__BUILD__/...` in `index.html` and import modules with relative paths. The server stamps the build id.
14. **Shared state lives in `state`.** State one module owns stays private to it. Another module that needs to change it calls a function that module exports.
15. **Build DOM with `el()`, `emptyRow()`, `titledRow()` and `fillList()`.** A row that appears in two lists is defined once in `rows.js`.
16. **A click handler whose only error handling is "show the message" uses `guarded()`.** A silent `catch {}` is for work that is truly best effort, and says so.
17. **The task grammar and the server vocabulary are never spelled out in a feature module.** Use `tasks.js` and `meta.js`.
18. **Keep `markdown.js`, `tasks.js`, `format.js` and `dom.js` free of network and app imports,** so their tests keep running under Node.

### Both

19. **Before adding a helper, search for an existing one.** Prefer widening a function over adding a parallel one.
20. **For a change that touches more than three files, write the plan first.**
21. **Keep refactors and behaviour changes in separate commits.** Pin current behaviour with a test before moving code.

## Tests

`python -m pytest -q` from `server/` runs everything:

- `tests/test_api.py`, `tests/test_push.py`: the API against a real PostgreSQL.
- `tests/test_design.py`: the invariants above (events after commit, one serializer, one grammar).
- `tests/test_migrations.py`: migrations from an empty database, and 0025 from an old one.
- `tests/test_static.py`: static guards on the client (wired ids exist, no hand written versions).
- `tests/test_client_js.py`: syntax checks every client script and runs `tests/js/*.test.mjs` under Node. No npm packages are involved.

`tests/task_grammar_cases.json` is read by both the Python and the JavaScript task tests. Add a case there, never in only one of them.

## Known constraints

- **One process.** Rate limits, pending WebAuthn challenges and the socket connection map live in memory (`security.py`, `ws.py`). Run a single worker. Scaling out needs those moved to shared storage first.
- **The native iOS push path is dormant.** `push.py`, `routes/devices.py` and `DeviceToken` stay a silent no op unless the relay settings are set. It sits behind `notify_user`, so it costs nothing while unused.
