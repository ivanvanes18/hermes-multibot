# Hermes Multibot

**Multiple Telegram bots. One Hermes profile. Separate conversations.**

A DM-only Hermes plugin that adds up to eight Telegram bot transports alongside
the bundled Telegram bot, in the **same gateway process**. Give each transport an
alias and its own allowlist. Replies and attachments leave through the bot that
received the conversation; session keys include the alias.

This is one shared **profile**, not one Python `AIAgent` object: Hermes creates
session-specific agents with the same memory, skill catalog and configured tools.
It is **not a security sandbox between admitted users**. Use separate Hermes
profiles when users must not share memory or authority.

## Status and compatibility

Version **0.2.0**, tested against Hermes **v0.23.0**. DM-only MVP. Offline
integration and failure tests pass. A live exchange from a real Telegram account
with two bots in one gateway (2026-11-09) confirmed: replies come from the bot
addressed, conversations stay separate, shared memory is visible to both, a photo
and a document reach the extra bot, and both conversations continue after a
gateway restart, with one reply per message. This does not claim long-running
production reliability or full bundled Telegram feature parity.

The plugin is an independent `BasePlatformAdapter` + python-telegram-bot transport,
registered through `register_platform`. It neither patches Hermes core nor forks,
subclasses or wraps the bundled Telegram adapter.

## Install

Prerequisite: Hermes >=0.23.0 with Telegram dependencies. The plugin's declared
Python dependency is `python-telegram-bot>=22.8,<23`; install it using Hermes's
normal dependency consent flow. No separate daemon or Telegram user-account login
is required.

For the current local/private checkout, copy **only `plugin/`** into the active
profile's plugin directory:

```sh
mkdir -p "$HERMES_HOME/plugins/hermes-multibot"
cp plugin/*.py plugin/plugin.yaml plugin/README.md plugin/LICENSE \
  "$HERMES_HOME/plugins/hermes-multibot/"
hermes plugins enable hermes-multibot
hermes plugins validate "$HERMES_HOME/plugins/hermes-multibot"
```

Set `HERMES_HOME` to your intended profile before these commands. Never copy the
project's tests or docs into the plugin directory.
A future approved catalog entry can be installed by catalog name; no public
catalog install is claimed today.

## Configure

Create extra bots with BotFather. Leave the bundled bot's `TELEGRAM_BOT_TOKEN`
unchanged. Configure non-secret slots in the active profile's `config.yaml`:

```yaml
plugins:
  enabled: [hermes-multibot]
  entries:
    hermes-multibot:
      enabled: true
      settings:
        bots:
          - slot: 1
            alias: work
            allowed_users: ['123456789']
          - slot: 2
            alias: personal
            allowed_users: ['123456789']
gateway:
  platforms:
    telegram: {enabled: true}          # optional existing bundled bot
    telegram_work: {enabled: true}
    telegram_personal: {enabled: true}
```

Merge these keys with your existing configuration; do not replace unrelated
plugin/platform entries. Slot is a unique integer **1–8**. Alias must be unique
and match `[a-z][a-z0-9_]{0,23}`. `allowed_users` is a nonempty list of **quoted
positive numeric Telegram user IDs**, not usernames, integers or wildcards.
Unknown bot-entry fields are rejected. Changing an alias changes its session key;
keep aliases stable to retain conversation continuity. Discovery happens at
cold start; restart **your intended gateway** after setup changes.

Store tokens through Hermes's profile secret settings (`hermes config`), or that
profile's protected `.env` file (mode `0600`): `MULTIBOT_SLOT_1_TOKEN` through
`MULTIBOT_SLOT_8_TOKEN`. Only configured slots need a token. **Never put tokens
in YAML, a command argument, this repository or a screenshot.** The manifest
declares all eight password fields. The plugin reads them exclusively with
`agent.secret_scope.get_secret`, honoring Hermes's active profile scope; the
plugin itself never opens a credential file. Missing secrets and malformed
config fail closed.

## What works

- Bundled Telegram plus extra bots in one gateway/profile.
- Bot-specific persisted session keys, e.g. `agent:main:telegram_work:dm:123456789`.
- Numeric per-bot intake ACL **in addition to** normal gateway authorization;
  global grants cannot widen that bot's allowlist. Denials happen before download.
- Text replies split to Telegram's 4096-character limit; standard base-adapter
  `send_image`, `send_image_file` and `send_document` delivery on the origin bot.
- Inbound individual Telegram photos and documents up to **5 MiB**, checked both
  in metadata and by a bounded streaming download. Supplier paths are discarded;
  safe document suffixes are retained on random filenames, document MIME is not
  trusted. Telegram photos are routed as images by the native gateway.
- Scoped token locks compatible with bundled Telegram, same-process duplicate
  guards, no forced takeover. Duplicate own-slot/bundled tokens are refused before
  polling; a failed acquire never releases another poller's lock.
- Start/stop/reconnect cleanup; bounded startup retries for network failures and
  PTB's normal polling backoff. Invalid-token and conflicting-poller errors park
  only the affected transport through the gateway's fatal-error interface.
- Persistent incoming replay suppression: **7 days / 10,000 claimed messages per
  token**. Claims commit before dispatch, so a crash after claim can lose a
  message. Expired/evicted replays can be delivered again. This is **not exactly
  once**. Changing a token creates a new replay store.

## Deliberate limits

**Private DMs only.** No groups, topics, albums, edited messages, voice/video,
streaming edits, model picker or complete bundled-command UX. Extra bot commands
are not promised to mirror bundled commands. No interactive tool-execution
approval buttons; a retained internal demo acknowledgement is non-executing,
one-use, bot/user/chat/message-bound and disappears on restart.

Media cache is capped at 32 files and a conservative 20 MiB per alias; stale
files (older than one day) are removed on the next accepted media intake. New
media is rejected when full; rejected media/download failures currently do not
produce a separate explanatory chat message. Document interpretation and vision
quality depend on Hermes/model/tools; transport support is not a claim of OCR or
model accuracy. No promise of delivery during network outages, crash recovery
without loss, or long-duration polling soak testing. Scope is one shared profile;
profile multiplexing interoperability is not independently qualified.

## Disclosure (catalog rules 9, 11 and 13)

- **Network:** Telegram Bot API polling, file download and outbound messages via
  python-telegram-bot/HTTPX. Inference remains the profile's normal Hermes route.
  No plugin analytics, telemetry, self-updater or remote executable downloads.
- **Credentials:** eight declared token secrets, accessed only through Hermes
  secret scope; tokens necessarily reach Telegram's SDK and authenticated API
  URLs. They are never deliberately logged. Keep Hermes secret redaction enabled.
- **Filesystem:** own replay SQLite stores and media cache under the active
  `HERMES_HOME`; standard Hermes scoped token locks. Outbound local attachments
  read the caller-supplied path through the base adapter's normal delivery path.
  Like any in-process plugin, this has the gateway's filesystem authority.
- **Background work:** one PTB long-poller per configured slot on the gateway
  event loop. No plugin-spawned shell processes or separate service.
- **Shared authority:** admitted users share profile memory, tools and skills.
  Per-bot allowlists protect intake, not data separation among those users.
- **Repository extras:** tests and docs live outside `plugin/` and are not
  installed by the catalog's `subdir: plugin`.

## Development and verification

The tests use real Hermes loader/config/GatewayRunner/AIAgent/session persistence,
real PTB objects and offline HTTP/LLM fixtures. DNS/socket connections are denied.
Run from the repository root, using your pinned Hermes interpreter and source:

```sh
env -i PATH=/usr/bin:/bin PYTHONPATH=/path/to/hermes-v0.23.0 \
  /path/to/hermes-v0.23.0/.venv/bin/python -m pytest tests -q
hermes plugins validate --json --install-deps ./plugin
```

The live-exchange checklist and its result are in `docs/LIVE_EXCHANGE.md`.
MIT licensed.
