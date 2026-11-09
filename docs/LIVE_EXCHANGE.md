# Live exchange — 2026-11-09

A real Telegram user account talked to two test bots served by **one** Hermes
v0.23.0 gateway and profile:

- **A** — the bundled Telegram adapter;
- **B** — this plugin (`telegram_extra` slot).

Model: a Claude Haiku-class model through the profile's normal inference route.
Agent tools were disabled on the test profile. One test word (`TANGERINE`) was
pre-written to the profile's shared memory.

| # | Check | Result |
|---|---|---|
| 1 | A: remember code ALPHA-ORANGE, answer A_OK, name the shared word | PASS — reply from A: `A_OK`, `TANGERINE` |
| 2 | B: answer B_OK, name the shared word and any code given earlier | PASS — reply from B: `B_OK`, `TANGERINE`, no code known (A's history did not leak) |
| 3 | A recalls its code; B stores BETA-LIME | PASS — A: `ALPHA-ORANGE`; B: `B_SAVED`; nothing appeared in A |
| 4 | Photo to B | PASS — saved under the plugin media cache with a random name; image reached the model (B described it) |
| 5 | Small `.txt` to B | PASS (transport) — saved with a random `.txt` name, byte-identical. The model saw the file reference only, because tools were off; reading documents is a tools/model matter, not transport |
| 6 | Session keys | PASS — `agent:main:telegram:dm:<user>` and `agent:main:telegram_extra:dm:<user>`, different session IDs, both persisted in `state.db` |
| 7 | Graceful stop → start; ask both for their codes | PASS — both reconnected within 10 s; B: `BETA-LIME`, A: `ALPHA-ORANGE`; same session IDs continued |
| 8 | Duplicates and pollers | PASS — exactly one reply per message (A 3/3, B 5/5); zero `getUpdates` conflicts; clean exit, no live process or bot-token locks after stop |

Observed once: the bundled adapter (A) needed about two minutes to connect at the
first start because `api.telegram.org` itself was timing out from the host at that
moment; the gateway's reconnection watcher recovered it on the same process. The
plugin slot connected normally.

Not claimed: live outgoing attachments from the bot (covered by offline tests),
long soak, groups/topics/voice.
