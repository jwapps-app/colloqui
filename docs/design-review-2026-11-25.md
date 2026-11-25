# Design Review: Colloqui (2026-11-25)

Scope: design complexity, not bugs. Reviewed at commit `365140c` on `main`. This is the first design review, so there is no earlier one to compare against.

File and line references below describe the code as it was at that commit, before the fixes.

## Status

All eight priorities were fixed on 2026-11-25, along with most of the optional items. The rules that came out of the work are in [design-principles.md](design-principles.md).

| # | Finding | Status | Where it lives now |
|---|---|---|---|
| 1 | `app.js` is one 4,442 line script | Done | 28 ES modules under `static/js/`, largest 445 lines. `app.js` is wiring only. |
| 2 | Asset version stamped by hand in five places | Done | `app/assets.py`. Assets are served under `/assets/<build>/`. No version number exists anywhere. |
| 3 | "Publish only after commit" applied by hand | Done | `db.after_commit`, `events.py`, and the `DB` dependency. |
| 4 | Posting a message has no single home | Done | `messaging.post_message` and the `publish_*` functions. |
| 5 | A message is serialized by hand at every endpoint | Done | `messaging.serialize_messages` with named `Part` choices. |
| 6 | Access rules written inline | Done | `access.py` and `membership.py`. No route module imports another. |
| 7 | Shared vocabulary spelled out twice | Done | `vocab.py`, `tasks.py`, `storage.py`, `GET /api/v1/meta`, `js/tasks.js`, `js/meta.js`. |
| 8 | `ChannelOut` built by two implementations | Done | `channel_out` calls `channels_out_bulk`. |

### What the work found beyond the review

- **The race in finding 3 is real.** A test that checks, at the moment an event is delivered, whether another connection can see the change failed on the old code for channel events.
- **Responses left before the commit too.** Creating a user and signing in as them at once failed during verification, because the session committed after the response was sent. The `DB` dependency now commits first. `tests/test_design.py` pins both.
- **A stale header after sign out.** The previous account's channel name and action buttons stayed on screen. Sign out now clears the channel view.
- **An inline style blocked by the content security policy** on the thread "Loading" placeholder. It is set through the DOM now.

### Behaviour that changed on purpose

Everything else is a move or an extraction with the same behaviour. These are the exceptions:

- Socket events, integration events, pushes and blob deletions now happen only after the commit. Before, most channel and space events were sent first.
- A request commits before its response is sent.
- A webhook post that is only whitespace is refused with 400. It used to store an empty message.
- Replaying an idempotent send for a message that has since been deleted returns a tombstone, not the deleted text.
- The database refuses unknown roles, notify levels and recurrences (migration 0025).

### Choices made explicit, left as they were

These differences existed before. They are now named in one place so they can be changed in one place. None was changed.

- **Pins and reactions are not sent to integrations.** Edits, checkbox changes and deletions are. See `tell_integrations` in `messaging.py`.
- **Link previews are not fetched for webhook posts.** See `fetch_previews` in `messaging.post_message`.
- **An admin must be a channel member to add members or manage webhooks,** but not to rename, move or remove members. See the header of `access.py`.
- **A thread participant whose level is "all" gets both a thread notification and a channel notification** for the same reply.

### Not done, and why

- **`INTEGRATION.md` and `PUSH_NOTIFICATIONS.md` were not moved into `docs/`.** `INTEGRATION.md` is a published contract path that other projects may link to.
- **`HANDOFF.md` and the September audit file were left alone.** Both are local files that git ignores. `HANDOFF.md` is still stale.
- **The dormant APNs path was kept.** Removing it is an owner decision. It is recorded under known constraints in the principles document.
- **`guarded()` exists and is used in the rebuilt lists,** but the other click handlers still use their own `try` and `catch`. Convert them as they are touched.
- **`test_api.py` was not split.** New tests went into `test_design.py`, `test_client_js.py` and `tests/js/`.

## Architecture summary

- **One deployable.** A single Docker image runs FastAPI (REST and WebSocket in one process) and serves the web client as static files. PostgreSQL 17 and a daily backup container sit beside it in Compose. Production is the NAS behind a Cloudflare Tunnel.
- **Server (`server/app`, about 9,200 lines of Python).** `main.py` wires middleware, security headers and routers. `config.py` is the one settings object. `db.py` owns the async engine and the `get_db` unit of work, which commits after the handler returns. `models.py` holds all 25 tables. `schemas.py` holds all Pydantic request and response models.
- **Routes are the domain layer.** There is no service layer. The 13 modules in `routes/` hold validation, permission checks, queries, business rules and event publishing inline. `messages.py` (1,269 lines), `channels.py` (880) and `auth.py` (862) carry most of it.
- **Realtime.** `ws.py` holds an in-process `ConnectionManager` that tracks sockets per user and fans events out. Handlers call `manager.send_to_users` directly with hand-built `{"type": ...}` dicts.
- **Notifications.** `notify.notify_user()` is the single dispatch seam. It writes the inbox row, sends the socket event, and schedules the two push transports: `webpush.py` (VAPID, live) and `push.py` (APNs via the push relay, dormant unless configured). `notify.reminder_loop()` is the only background job.
- **Integrations.** Incoming webhooks post messages (`routes/webhooks.py`). Outgoing webhooks deliver signed events (`webhooks_out.py`). API keys act as a user (`deps.py`). `GET /sync` exposes the message change log. The contract is in `INTEGRATION.md`.
- **Web client (`server/app/static`).** No framework and no build step. `index.html` (500 lines) holds every view. `app.js` (4,442 lines) is one classic script holding all state, rendering, API calls and event wiring. `sw.js` handles push and an offline shell cache.
- **Main concepts and where they live.** User, Session, credentials (passkey, password, TOTP), Invite, Space, SpaceMember, Channel (also DMs), ChannelMember, Message (also threads, pins, task lines), Reaction, File, Reminder, Notification, Webhook, ApiKey, EventSubscription. Tables are in `models.py`, shapes in `schemas.py`, rules in the matching `routes/*.py`.
- **Tests.** 61 tests run against a real Postgres: `test_api.py` (44), `test_push.py` (13), `test_static.py` (3), `test_migrations.py` (1). CI runs pytest and a syntax check of `app.js`. The client has no behavioural tests.

### Hotspots (size and churn, 82 commits total)

| File | Lines | Commits |
|---|---|---|
| `static/app.js` | 4,442 | 51 |
| `static/index.html` | 500 | 50 |
| `tests/test_api.py` | 1,361 | 23 |
| `static/style.css` | 768 | 22 |
| `schemas.py` | 478 | 17 |
| `models.py` | 491 | 15 |
| `routes/messages.py` | 1,269 | 10 |
| `routes/channels.py` | 880 | 10 |

## Overall assessment

The foundations are strategic: one settings object, one notification seam, credentials separated from identity, reproducible builds, a migration round trip test, and comments that record why. The route modules and the client are tactical. Rules live inside individual handlers instead of in one named place, so each rule gets fixed one handler at a time. The September audit shows this directly: the broadcast before commit bug, the partial message payloads, the events that never reached integrations and the duplicate channel names were each one instance of a rule that had no single home. The largest cost by far is the client, where one file absorbs almost every change and nothing in it can be tested. None of this needs a rewrite. Each item below is a move or an extraction that leaves the app working after every step.

### Change amplification traces

- **Ship any change to `app.js` or `style.css`.** Also edit `APP_VERSION` in `app.js` and three stamps in `index.html`. 50 of the 52 commits that touched the script or stylesheet also touched `index.html`.
- **Add a field to a message** (for example "forwarded from"). `models.py`, a migration, `MessageOut` in `schemas.py`, the 8 parameter `message_out()`, up to 9 assembly sites in `messages.py`, the webhook ingest path, `buildMessageNode` and `handleEvent` in `app.js`, and `INTEGRATION.md`.
- **Let space managers manage channels in their space.** `require_manageable` and the inline check in `add_member` (`channels.py`), `require_channel_manager` (`webhooks.py`), the inline check in `delete_message` (`messages.py`), and `canManage` in `app.js`. Four files, five sites, and the existing sites already disagree with each other.
- **Add a count to the sidebar.** `ChannelOut`, both `channel_out` and `channels_out_bulk`, then `channelLi`, the collapsed space rollup, and the hand maintained counters in `handleEvent`.

## Top priorities

### 1. `app.js` is one 4,442 line script (done)

- **Where:** `server/app/static/app.js`. 27 sections, 37 top level `let` globals, 221 `document.createElement` calls, wiring from line 4044.
- **Symptom:** god file, duplicated logic, no seam for testing.
- **Why it matters:** 51 of 82 commits touch this file, so every feature pays its cost. The same list is rendered twice in several places: threads in `loadInfoPane` (1428) and `loadThreadsInbox` (2915), pins in `loadInfoPane` and `loadPins` (2864), tasks in `loadInfoPane` and `loadTasks` (2801). The empty state row is built by hand 13 times and the "reply or replies" label 5 times. `handleEvent` (3046) patches `message_count`, `recent_count` and `unread_count` by hand in parallel with the server. Because the script wires the DOM at load, none of it can run under a test, including the pure pieces such as the markdown renderer (`renderInline`, `renderBody`). The only guard is three regex checks in `test_static.py`.
- **Fix:** split into native ES modules. No bundler is needed, and the PDF.js loader is already a module. Do item 2 first so imports are cache busted.
  1. Extract the pure pieces first: `format.js` (`relTime`, `shortDate`, `fmtSize`), `markdown.js` (`renderInline`, `renderBody`, `serializeEditor`), `api.js`. Add `node --test` cases for the markdown round trip and run them in CI.
  2. Add two small helpers and use them as you touch code: `el(tag, props, children)` and `listPane(listEl, items, {empty, row})`. Rebuild the tasks, pins and threads lists on them so each row is defined once and used by both the info pane and the inbox.
  3. Move state into one `state.js` module (`me`, `channels`, `spaces`, `currentChannel`, presence).
  4. Then move one section per change: `realtime.js`, `messages.js`, `panes.js`, `settings.js`, `admin.js`. Keep `app.js` as the entry that imports and wires.
- **Pain / Frequency / Effort:** High / High / L, in small steps

### 2. The asset version is stamped by hand in five places (done)

- **Where:** `app.js:20` (`APP_VERSION = '125'`), `index.html:15`, `:16`, `:498` (`?v=125`), `index.html:497` and `app.js:1617` (PDF.js `?v=124`), `index.html:12-14` (icons `?v=81`). `checkForUpdate` (`app.js:21`) parses the number back out of the HTML.
- **Symptom:** information leakage, change amplification.
- **Why it matters:** every client change needs four coordinated edits or users run stale code against a year long immutable cache. 36 of the 50 commits to `index.html` changed 8 lines or fewer, which is mostly this. `test_asset_versions_match` guards two of the five stamps. It also blocks item 1, because `import './x.js'` carries no `?v=`.
- **Fix:**
  1. At startup compute one build id: `APP_VERSION` when set, otherwise a short hash of the static files.
  2. Mount static assets under `/assets/<build>/` and serve `index.html` from a small handler that substitutes the id. Relative module imports then inherit the versioned path with no stamps at all.
  3. Return the build id from `/healthz` (it already returns `version`) and have `checkForUpdate` compare against that.
  4. Delete `APP_VERSION` from `app.js` and the stamps from `index.html`. Replace the static test with one that asserts no literal `?v=` remains.
- **Pain / Frequency / Effort:** Med / High / S

### 3. "Publish only after commit" is applied by hand, handler by handler (done)

- **Where:** `db.py:29-36` (`get_db` commits after the handler returns). `routes/messages.py` has 8 explicit `await db.commit()` calls placed before each broadcast. `routes/channels.py` and `routes/spaces.py` have 12 functions that send socket events after `flush()` with no commit: `create_channel`, `update_channel`, `move_channel`, `reorder_channels`, `add_member`, `remove_member`, `open_dm`, `_ensure_channel_owner`, `update_space`, `add_space_member`, `remove_space_member`, `_ensure_space_manager`.
- **Symptom:** a rule with no single home, error handling by convention.
- **Why it matters:** the client answers `channels.changed` by refetching at once (`app.js:3073`), so it can read the state from before the commit, and a failed commit leaves every client told about a change that never happened. The audit fixed exactly this for messages. The same rule is still unenforced for channel and space events, and every new handler has to remember it. I did not reproduce the race. It follows from the code order.
- **Fix:**
  1. Add `events.publish(db, user_ids, payload)` that appends to `db.info["outbox"]`.
  2. In `get_db`, deliver the outbox after a successful commit and drop it on rollback. Do the same for `dispatch_event` and `schedule_previews`.
  3. Convert handlers one at a time, starting with `channels.py`. Each conversion is a one line change.
  4. Once `messages.py` is converted, remove its manual commits.
- **Pain / Frequency / Effort:** High / Med / M

### 4. Posting a message has no single home (done)

- **Where:** `routes/messages.py:871-1002` (`send_message`), `routes/webhooks.py:99-143` (`ingest`), and the mutation handlers at `messages.py:1021`, `1102`, `1144`, `1179`, `1217`.
- **Symptom:** tangled responsibilities, duplicated logic, route modules importing each other's privates.
- **Why it matters:** `send_message` validates, persists, stamps the change log, broadcasts, dispatches the webhook event, schedules previews, runs 40 inline lines of thread notification (959-1000) and then calls `_notify_for_message`. Webhook ingest rebuilds a subset by importing `_notify_for_message`, `broadcast`, `message_out` and `record_change` from the route module (`webhooks.py:16`), and it skips link previews. The sequence "record change, serialize, commit, broadcast, dispatch" is repeated in five handlers with differences nobody chose: pins and reactions never reach integrations, edits were added late. Any new message source (a CRM post, a scheduled message) or any new side effect (at rest encryption, which is on your roadmap) has to be threaded through each copy.
- **Fix:**
  1. Create `app/messaging.py`. Move `record_change`, `broadcast`, `_notify_for_message` and the thread notification block into it unchanged.
  2. Add `post_message(db, channel, sender, content, *, file=None, reply_to_id=None, thread_root_id=None, webhook_name=None, client_id=None)` that owns the whole sequence. Call it from `send_message` and from `ingest`.
  3. Add `publish_message_update(db, message, event)` and call it from the checkbox, pin, edit, reaction and delete handlers.
  4. Parse mentions once and pass the set to both notification paths (today the regex runs twice, at `:818` and `:965`).
- **Pain / Frequency / Effort:** High / Med / M

### 5. A message is serialized by hand at every endpoint (done)

- **Where:** `routes/messages.py`: `message_out` (70, eight positional parameters) and the five loaders `reactions_for`, `completions_for`, `reply_previews_for`, `thread_meta_for`, `previews_for`, assembled at `598-609`, `635-661`, `739-753`, `888-894`, `1013-1018`, with partial versions at `545-556`, `696-702` and `1087-1098`, and bare versions in search and the cross channel pins feed.
- **Symptom:** duplicated logic, an interface as wide as its implementation.
- **Why it matters:** each endpoint decides which parts to load, and callers pass them by position (`message_out(m, u, f, aggs.get(m.id), comps.get(m.id), None, threads.get(m.id))`). A new field on `MessageOut` means visiting up to nine call sites, and a missed one ships a message with part of its state blank. The audit's partial payload finding came from this, and `full_message_out` fixed it for one message only.
- **Fix:**
  1. Add `serialize_messages(db, rows, *, include=FULL) -> list[MessageOut]` in `app/messaging.py`, taking `(Message, User, File)` rows and running the five loaders once.
  2. Make `message_out` keyword only.
  3. Replace the call sites one at a time. Define `full_message_out` as the single row case.
  4. Decide once which feeds are deliberately light (search, pins) and name that choice with an `include=` constant.
- **Pain / Frequency / Effort:** Med / Med / S

### 6. Access rules are written inline, with different answers in different files (done)

- **Where:** "channel owner or admin" appears in `channels.py:64` (`require_manageable`, an admin who is not a member passes), `webhooks.py:29` (`require_channel_manager`, an admin must be a member), `channels.py:746-748` (inline in `add_member`) and `messages.py:1229-1231` (inline in `delete_message`). The space manager check in `channels.py:481-484` repeats `spaces.py:89`. Space membership is checked inline at `channels.py:452`, `511`, `611`, `710` and `755`. Private helpers cross route modules through function level imports at `admin.py:94-95`, `spaces.py:298`, `auth.py:331` and `auth.py:433`.
- **Symptom:** duplicated logic, information leakage.
- **Why it matters:** changing one permission rule means searching five files and reconciling versions that already differ. The function level imports show the route modules depend on each other in a cycle.
- **Fix:**
  1. Create `app/access.py`. Move `require_member`, `require_manageable`, `require_space_member` and `require_space_manager` there unchanged and update imports.
  2. Add `is_channel_manager(db, channel, user)` and `require_space_access(db, space_id, user)`. Replace the inline checks one handler at a time.
  3. Make the webhook rule and the channel rule the same function, and choose on purpose whether an admin must be a member.
  4. Move `_ensure_channel_owner`, `_ensure_space_manager`, `add_to_space` and `ensure_default_space` into `app/membership.py` so no route module imports another.
- **Pain / Frequency / Effort:** Med / Med / S

### 7. Shared vocabulary is spelled out separately on the server and the client (done)

- **Where:**
  - Task line grammar in 13 places with three spellings (`^\[ \] .`, `^\[( |x)\] `, `[ xX]`): `messages.py:447`, `456`, `1042`; `channels.py:48`, `159`, `366`; `app.js:1756`, `1785`, `1998`, `2343`, `3043`, `4102`, `4108`.
  - Reaction emoji: `messages.py:62` and `app.js:2421`.
  - Notify levels: `schemas.py:164`, `channels.py:43`, `messages.py:808`, `846`, `989`, `app.js:1302`.
  - Recurrence: `schemas.py:435` and `:466`, `notify.py:43-50`, `calsync.py:104`, `index.html:435`.
  - Roles `owner`, `manager`, `member` as bare strings throughout.
  - Blob path `Path(settings.upload_dir) / str(file_id)` built in 7 places: `files.py:35`, `95`; `spaces.py:211`; `admin.py:104`; `messages.py:1243`; `channels.py:695`; `users.py:22`.
- **Symptom:** information leakage.
- **Why it matters:** the server counts open tasks in SQL with one pattern and the client draws checkboxes with another, so the sidebar count and the visible boxes can drift apart. Adding an emoji, a notify level or a recurrence means matching edits in two languages with nothing to catch a miss. Moving uploads (for example to encrypted storage) means finding seven path expressions.
- **Fix:**
  1. `app/tasks.py`: `TASK_LINE`, `is_task_line`, `open_task_lines(content)`, `set_checked(content, line, checked)`, and the SQL expression. Use it from `messages.py` and `channels.py`.
  2. `app/storage.py`: `blob_path(file_id)`, `avatar_path(user_id)`, `unlink_blobs(ids)`.
  3. `app/vocab.py`: `NotifyLevel`, `Recurrence`, `ChannelRole`, `SpaceRole` as `StrEnum`, used by the schemas instead of regex patterns.
  4. `GET /api/v1/meta` returning the emoji list, levels and recurrences. The client reads it at startup and drops `EMOJI_SET` and the hardcoded options.
  5. On the client, one `tasks.js` with the single regex, once item 1 has modules.
- **Pain / Frequency / Effort:** Med / Med / M

### 8. `ChannelOut` is built by two parallel implementations (done)

- **Where:** `routes/channels.py:94-211` (`channel_out`, about nine queries for one channel) and `:251-427` (`channels_out_bulk`, grouped queries). The docstring says "Same output as channel_out()".
- **Symptom:** duplicated logic.
- **Why it matters:** nothing enforces "same output". A new count has to be written twice in two query styles, and the single channel version is returned by eight endpoints, so a drift shows up as a sidebar number that changes after any edit.
- **Fix:** replace the body of `channel_out` with `return (await channels_out_bulk(db, [channel], me))[0]` and delete about 115 lines. Add one test that creates a channel with a pin, a thread, a task and an unread message and compares the single result with the list result.
- **Pain / Frequency / Effort:** Med / Med / S

### Suggested order

8, then 6, then 2 (each small and independent). Then 3, then 4 and 5 together since they share a module. Then 7. Item 1 runs alongside from the moment 2 lands, one section at a time.

## Optional / later

- **Registration is written twice.** `register_verify` (`auth.py:267`) and `register_password` (`auth.py:409`) both do recover or create, join the default space, consume the invite and issue a session. Extract `_complete_registration`. **Outcome:** Done. `_account_for_registration` and `_finish_registration` in `routes/auth.py`.
- **Five copies of the fire and forget task pattern.** `ws.py:30`, `push.py`, `webpush.py`, `webhooks_out.py:103`, `messages.py:330-347` each keep their own strong reference set. One `background.spawn(coro)` would do. **Outcome:** Done. `background.spawn`.
- **`webpush.send_test` repeats `_send`** (`webpush.py`), with the same pywebpush call and error mapping. **Outcome:** Done. Both call `webpush._post`.
- **The APNs path is dormant but still maintained.** `push.py` (311 lines), `routes/devices.py`, `DeviceToken`, three settings and `test_push.py`. The README says there is no native app, yet the latest commit changed this code. Decide whether it stays. It is cleanly behind `notify_user`, so keeping it costs little as long as that is a choice. **Outcome:** Kept. Owner decision.
- **`HANDOFF.md` is stale.** It describes the iOS app and push as future work, passkeys as the only login, and five route modules where there are thirteen. Delete it or regenerate it, and keep the README as the truth. **Outcome:** Not changed. It is a local file that git ignores.
- **A one time data migration still runs at every startup.** `migrate_existing_to_default_space` (`main.py:37-69`) finished its job in June. **Outcome:** Done. It is migration 0025 now.
- **Constraints the app enforces only in code.** Roles, notify levels and recurrence are free strings in the database. `Space.created_by` is `NOT NULL` with `ON DELETE SET NULL` (`models.py:152`), which `admin.py:71-75` works around. **Outcome:** Done. Migration 0025 adds the CHECK constraints and makes `spaces.created_by` nullable.
- **Client error handling is ad hoc.** 55 copies of `catch (e) { appAlert(e.message) }` and 32 silent `catch {}`. A `guarded(fn)` wrapper would make the choice explicit. **Outcome:** Partly done. `guarded()` exists and the rebuilt lists use it.
- **No linter or type checker in CI.** The `# noqa` comments suggest ruff is run by hand. Adding `ruff check` to CI is cheap. **Outcome:** Done for linting. `server/ruff.toml`, run in CI.
- **`test_api.py` is one 1,361 line file.** Split it by area as you touch it. **Outcome:** Not split. New tests live in their own files.
- **In process state pins the server to one worker.** `RateLimiter`, `ExpiringStore` and `ConnectionManager` live in memory. That is fine at this scale. Write it down as a constraint. **Outcome:** Done. Written down in the principles document.
- **Docs are scattered at the repo root.** `HANDOFF.md`, `INTEGRATION.md`, `PUSH_NOTIFICATIONS.md` and `colloqui-code-audit.md` could move into `docs/`. **Outcome:** Not moved. See "Not done, and why".

## What's already good

- **`notify_user()` is a real seam.** Producers do not know about transports, and each transport is a silent no op until configured.
- **One settings object.** Nothing else reads the environment.
- **Identity, credentials and sessions are separate tables.** Password and TOTP were added without touching the rest.
- **Bulk loaders avoid N+1 queries.** `channels_out_bulk` and the `*_for` helpers in `messages.py` are the right shape. Items 5 and 8 only ask that they be the single path.
- **Outbound requests share one guard.** `links.pin_url` is reused by link previews and outgoing webhooks.
- **Migrations are tested from an empty database in both directions**, and the image builds from a hash pinned lock.
- **Tests run against real Postgres**, so the SQL specific features are actually exercised.
- **Comments record why.** Nearly every non obvious line says what went wrong before. Keep doing this.
- **The client already has good single sources** to copy: one `api()` helper, one dialog system, and the `acts` list in `buildMessageNode` that feeds both the hover icons and the long press menu.

## Design principles

The rules this review led to are in [design-principles.md](design-principles.md). They name the modules that now exist, so they are the document to read before adding a feature.
