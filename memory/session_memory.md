# Agent Context Snapshot

## 1. Work State

### Completed
- Environment reconnaissance: Ubuntu container, hostname `292ffc035821`, running as root, Python 3.12.3.
- Installed packages confirmed: `mailman3 3.3.8-3.1ubuntu0.1` (all) and `postfix 3.8.6-1build2` (amd64).
- Mailman Python package at `/usr/lib/python3/dist-packages/mailman/` (3.3.8). Entry points: `mailman`, `master`, `runner` → `mailman.bin.mailman:main`, etc.
- Existing config `/etc/mailman3/mailman.cfg` (mode `-rw-r----- root:list`, 11115 bytes) — Debian layout, sections at lines: `[mailman]`=23, `[shell]`=86, `[paths.debian]`=108, `[database]`=160, `[logging.debian]`=178, `[webservice]`=218, `[mta]`=241.
- Key config values currently: `var_dir: /var/lib/mailman3`, `data_dir: $var_dir/data`, `database.url: sqlite:///$DATA_DIR/mailman.db`, `incoming: mailman.mta.postfix.LMTP`, `outgoing: mailman.mta.deliver.deliver`, `smtp_host: localhost`, `smtp_port: 25`, `lmtp_host: 127.0.0.1`, `lmtp_port: 8024`, `configuration: python:mailman.config.postfix`.
- `/app/eval.py` read in full. Tests: `test_simple_local_delivery()`, `test_mlist_exists()`, `test_join_flow()`. `test_join_flow` expects join flow: send to `reading-group-join@local.edu`, assert NOT member → confirmation `From:` starts with `reading-group-confirm` and subject contains "join" arrives in `/var/mail/<user>` → user replies to that From → assert member.
- **Postfix `main.cf` EDITED successfully** via `edit_file`: replaced lone `mydestination` line with:
  ```
  mydestination = $myhostname, localhost, localhost.localdomain, , localhost, local.edu
  transport_maps = hash:/var/lib/mailman3/data/postfix_lmtp
  relay_domains = hash:/var/lib/mailman3/data/postfix_domains
  ```
  Result: `occurrences_replaced: 1, bytes_written: 1465`.
- Discovered baseline Postfix config: `alias_maps = hash:/etc/aliases`, `local_recipient_maps = proxy:unix:passwd.byname $alias_maps`, `mailbox_size_limit = 0`, `recipient_delimiter = +`, `myhostname = localhost.local`, `mail_spool_directory = /var/mail`, `/var/spool/mail -> ../mail`.
- Mailman internals mapped:
  - `mta/postfix.py` (`LMTP` class): `regenerate()` writes `postfix_lmtp`, `postfix_domains`, `postfix_vmap` into `config.DATA_DIR` (=`/var/lib/mailman3/data`), then runs `postmap` on them. Format: `ALIASTMPL = '{0:{2}}lmtp:[{1.mta.lmtp_host}]:{1.mta.lmtp_port}'`.
  - `mta/aliases.py`: `SUBDESTINATIONS = (bounces, confirm, join, leave, owner, request, subscribe, unsubscribe)`.
  - `runners/lmtp.py`: `SUBADDRESS_NAMES` maps `confirm→confirm`, `join→join`, `leave→leave`, `subscribe→join`, `unsubscribe→leave`; `SUBADDRESS_QUEUES` maps `confirm/join/leave/request→command`, `bounces→bounces`, `owner→in`. Listens on `lmtp_host:lmtp_port`.
  - `app/subscriptions.py`: `SubscriptionWorkflow` — if `address.verified_on is None` → `send_confirmation` (so join needs confirm regardless of policy). `confirm_address(cookie)` in `model/mailinglist.py` renders `verp_confirm_format: $address+$cookie` → `reading-group-confirm+<cookie>@local.edu`. `_handle_confirmation_needed_events` sets `From: <confirm_address>`, subject "Your confirmation is needed to join the ${fqdn_listname} mailing list." (uses `verp_confirmations: yes` default from `config/schema.cfg` line 889).
  - `commands/eml_confirm.py` (`Confirm`), `commands/eml_membership.py` (`Join`/`Leave`), `runners/command.py` (`CommandFinder` uses `config.mta.verp_confirm_regexp: ^(.*<)?(?P<addr>[^+]+?)\+(?P<cookie>[^@]+)@.*$` for `-confirm` subaddress).
  - `styles/base.py`: defaults set `subscription_policy = SubscriptionPolicy.confirm`, `unsubscription_policy = SubscriptionPolicy.confirm` (requirement: set `SubscriptionPolicy.open`).
  - `SubscriptionPolicy` enum in `interfaces/mailinglist.py` line 80: `open=0, confirm=1, moderate=2, confirm_then_moderate=3`.
  - `app/lifecycle.create_list(fqdn_listname, owners, style_name)` creates list + calls `call_name(config.mta.incoming).create(mlist)` (regenerates maps).
  - CLI: `mailman --run-as-root -C /etc/mailman3/mailman.cfg create <list@domain>` (flags `-D/--no-domain`, `-o/--owner`, `-q/--quiet`, `-n/--notify`).
  - `mailman --run-as-root -C /etc/mailman3/mailman.cfg info` works; `lists` → "No matching mailing lists found".

### Active (In-Progress)
- Just applied `main.cf` edit. Have NOT yet: verified `postconf -n` reflects it, run `postmap` on the (not-yet-existing) map files, started postfix/mailman3, created the `reading-group@local.edu` list, set policies, or written the manual postfix maps.

### Blocked / Failure Lessons
- `service postfix status` and `service mailman3 status` → both "is not running" (exit code 3 for postfix). Daemons must be started manually; no systemd (SysV init scripts `/etc/init.d/postfix`, `/etc/init.d/mailman3`).
- `/var/lib/mailman3/data/` contains only `mailman.db` (270336 bytes) — **`postfix_lmtp`, `postfix_domains`, `postfix_vmap` do NOT exist yet**. Postfix `transport_maps`/`relay_domains` referencing them will fail lookups until Mailman regenerates them (or they're created manually + `postmap`ed).
- `/etc/aliases` only has `postmaster: root` — no mailman aliases yet.
- `mailman` CLI refuses root without `--run-as-root` (must be passed before subcommand).
- `mailman domains` is NOT a command (exit code 1 "No such command 'domains'").
- Man pages stripped ("system minimized") — `man 5 transport` unavailable; `/usr/share/doc/postfix/README.Debian.gz` does NOT exist despite `dpkg -L` listing it.
- `pip list` shows no mailman entry (it's a dist-packages install, not pip-managed).
- `/var/lib/mailman3/lists`, `archives`, `cache`, `messages`, `templates`, `queue` exist; `/run/mailman3/` exists `drwxr-xr-x list:list`; `/var/log/mailman3` exists `list:list`.
- Repeated `read_file` on `/app/eval.py` (7+) and `mta/postfix.py` has burned budget without new info — avoid re-reading; content is known.

## 2. Next Move
1. Verify the `main.cf` edit: `postconf -n`.
2. Update `/etc/mailman3/mailman.cfg` (`[mailman]` section) — e.g. `site_owner`, plus any needed `[mta]` values — and create the mailing list via
   `mailman --run-as-root -C /etc/mailman3/mailman.cfg create -D -q reading-group@local.edu`
   (or pre-insert a Domain for `local.edu` first, then create the list).
3. Set `subscription_policy = open` and `unsubscription_policy = open` on the list (via `mailman shell`/`withlist` or a Python script using `initialize('/etc/mailman3/mailman.cfg')` + `IListManager.get('reading-group@local.edu')`).
4. Ensure `postfix_lmtp` (+ `.db`) exists and contains `reading-group@local.edu` and all sub-addresses (`-join`, `-leave`, `-confirm`, `-bounces`, `-owner`, `-request`, `-subscribe`, `-unsubscribe`) → `lmtp:[127.0.0.1]:8024`; run `postmap` on it. Same for `postfix_domains` (`local.edu`).
5. Start services: `service postfix start` (or `postfix start`), `service mailman3 start` (runs `/usr/bin/mailman -C /etc/mailman3/mailman.cfg start` as user `list`).
6. Validate with `python3 /app/eval.py` (the file-level test command); iterate on failures.

## 3. Working Context & Anchors
- **Relevant Files / Artifacts**:
  - `/app/eval.py` — the evaluator (tests `test_simple_local_delivery`, `test_mlist_exists`, `test_join_flow`); uses `MAILING_LIST_CONFIG = "/etc/mailman3/mailman.cfg"`, `DOMAIN_NAME = "local.edu"`, addresses `reading-group@local.edu`, `reading-group-join@local.edu`, `reading-group-leave@local.edu`; reads mailboxes via `mailbox.mbox(f"/var/mail/{username}")`; sends via `smtplib.SMTP("localhost", 25)`.
  - `/etc/mailman3/mailman.cfg` — **required** output location for Mailman config edits (mode `640 root:list`).
  - `/etc/postfix/main.cf` — EDITED (transport_maps/relay_domains/mydestination).
  - `/etc/postfix/master.cf` — has `smtp inet ... smtpd` (port 25) and `lmtp unix ... lmtp`; no changes made.
  - `/usr/lib/python3/dist-packages/mailman/mta/postfix.py`, `mta/aliases.py`, `runners/lmtp.py`, `runners/command.py`, `app/subscriptions.py`, `model/mailinglist.py`, `commands/eml_confirm.py`, `commands/eml_membership.py`, `styles/base.py`, `config/postfix.cfg`, `config/schema.cfg`.
  - `/var/lib/mailman3/data/` — Mailman DATA_DIR; target dir for `postfix_lmtp`/`postfix_domains`/`postfix_vmap`.
  - `/etc/aliases` (hash DB at `/etc/aliases.db`), `newaliases` available.
- **Environment State**:
  - `main.cf` now contains: `mydestination = $myhostname, localhost, localhost.localdomain, , localhost, local.edu`; `transport_maps = hash:/var/lib/mailman3/data/postfix_lmtp`; `relay_domains = hash:/var/lib/mailman3/data/postfix_domains`.
  - Postfix and Mailman3 services **stopped**.
  - List `reading-group@local.edu` **does not exist** (`mailman lists` empty).
  - No mailboxes in `/var/mail/` yet; users are created on demand by `eval.py` via `useradd`.
  - Mailman DB: `/var/lib/mailman3/data/mailman.db` (SQLite).
  - `list` user uid=38 gid=38.
  - Budget: 102/120 used — very little remaining; must act decisively with few tool calls.