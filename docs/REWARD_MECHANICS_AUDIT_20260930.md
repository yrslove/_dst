# DST reward mechanics audit — 2026-09-30

## Target and evidence boundary

Audited the installed DST build `747465` / Steam build `24700692`, using its
`data/databundles/scripts.zip`, current Runtime Agent heartbeat, current worker
telemetry, a retained live in-world screenshot, and the item-server health endpoint.
The client Lua does not include the native `TheInventory` or Klei item-server
implementation, so server-side eligibility and timers remain opaque.

## Two distinct reward paths

### Daily/login gift

`scripts/screens/redux/networkloginpopup.lua` waits for account login and inventory
download, including `INVENTORY_PROGRESS.CHECK_DAILY_GIFT`. The multiplayer main menu
then combines `TheInventory:GetUnopenedEntitlementItems()` with
`GetDailyGiftItem()` and shows `ThankYouPopup`; daily items are tagged
`DAILY_GIFT` in `scripts/screens/redux/multiplayermainscreen.lua`. This is a lobby
login/inventory path. It is not the in-world GiftItemToast path.

The local source shows no daily reset timestamp, next-due endpoint, or account-level
daily state. A successful login and `items.kleientertainment.com/HealthCheck = OK`
do not prove that today's gift was eligible or received.

### In-world playtime gift

The player `giftreceiver` initializes from the native `TheInventory:GetClientGiftCount`
and refreshes on `ms_updategiftitems` (commented as originating in native
`GiftingManager.cpp`). `hasgift` is replicated separately from `hasgiftmachine` via
`player_classified.lua`.

The gift machine is set by `builder.lua` only when the current prototyper has the
`giftmachine` tag, is visible to the player, and `inventory.isopen` is true. The
Science Machine and Alchemy Engine use the `scienceprototyper` implementation that
adds this tag. A nearby, visible Science Machine/Alchemy Engine and open inventory
are therefore required to make the gift button actionable. The code also tags
Bookcase as a gift machine; it is not required for the prepared fixture.

`scripts/components/inventory_replica.lua` schedules `components.inventory:Open()` at
player creation, so a normal fresh player starts with the server inventory open.
`CONTROL_OPEN_INVENTORY` only toggles controller inventory UI, while
`CONTROL_OPEN_CRAFTING` toggles the crafting UI; neither input directly opens the
server inventory component or establishes a nearby giftmachine. A visible gray toast
is live evidence that `hasgiftmachine` is currently false. The prepared Science Machine
is visible below the character in the live screenshot, so the remaining source
conditions point to its current range/visibility context; the screenshot does not prove
the exact player-to-machine distance.

`GiftItemToast` shows while `numitems > 0`; its click is enabled only when
`hasgiftmachine` is true. A gray visible present therefore means a gift is pending
but the machine context is not currently enabled. `gray == NO_REWARD_AVAILABLE` is
incorrect for this build. The old 48m06s gray-icon segment did not test whether a
gift was pending and cannot be counted as a no-gift/AFK eligibility experiment.

The claim path is distinct from the daily modal: clicking the toast sends
`RPC.OpenGift`; the server-side `giftreceiver:OpenNextGift()` enters the
`opengift` state; `SGwilson.lua` opens `POPUPS.GIFTITEM`; the popup reveals the item
and offers `Use Later` / `Use Now`. Closing the popup emits `ms_closepopup`, which
ends the opening state. The client pauses the world during the reveal. A click,
animation, or disappearing toast alone is not durable confirmation.

## What source/runtime does and does not establish

- There is no Lua-side AFK detector, last-input timestamp, or eligible-play counter
  in the audited path. The drop count and timing come from native inventory/item
  service code unavailable in the scripts bundle.
- The player was online in a loaded world; the item-server public health endpoint
  returned `OK` at 2026-09-30 13:50 UTC. This tests endpoint health, not this account's
  authenticated reward session or server-side playtime eligibility.
- Current Runtime Agent heartbeat at 13:50 UTC reported `GAME_READY`, DST and Steam
  running, `WORLD_PROFILE_VERIFIED`, worker `ACTIVE`, no held inputs, and no worker
  gameplay actions during the current worker run. It is not proof that Klei counted
  eligible time.
- Community references report up to eight randomized in-world drops per weekly
  cycle, requiring online play and opening the prior gift before time toward the next
  starts; one 2019 forum reply estimates roughly 50–80 minutes per drop and says
  timing varies by account/week. These are community reports, not build source or
  account-specific proof. Sources:
  [Klei Forums skin checklist discussion](https://forums.kleientertainment.com/forums/topic/68348-dst-skins-checklist-updated-02122025/page/3/),
  [Curio Cabinet reference](https://dontstarve.wiki.gg/wiki/Curios).
- The community reference lists weekly reset around Thursday 1pm Pacific / Klei
  time, but exact current reset instant and account progress remain unverified.
- Sources conflict about whether AFK qualifies: an older Steam discussion says
  activity is unnecessary, while the current wiki says players must be active. The
  installed source cannot resolve what Klei's native service treats as activity.
- No movement is inherently required to claim when the character is already visible
  to a giftmachine with the inventory open. Claim still requires canonical UI input.

## Current live state after the active-present capture

The worker correction for gray pending icons remains deployed: it reports
`IN_WORLD_GIFT_PENDING`, not `NO_REWARD_AVAILABLE`, and keeps a separate
`inworld_gift_state`. Source inspection established that a fresh player starts with its
server inventory open. `Tab` only toggles controller inventory UI and `b` toggles the
crafting UI. The initial player position `(369.599, 193.648)` was 4.99 units from the
prepared Science Machine `(374, 196)`, outside the 4-unit research-machine radius.
The one-shot 0.45-second canonical station approach was deployed; two steps were sent
and each fresh gameplay ROI verified. An active pink/red gift appeared afterward.

A real 1280x720 frame and 58x58 active gift crop were captured at 14:30:30 UTC. The
active template scores 0.99994; the gray template scores 0.91665 and misses, causing the
deployed classifier to return UNKNOWN for a valid in-world HUD. Commit `003e1fa` adds
the regression patch, which classifies the retained frame as
`IN_WORLD_IDLE` plus `GIFT_AVAILABLE` offline while daily login state remains UNKNOWN.
It also separates in-world opening/received telemetry from daily state. Deployment
metadata now names `003e1fa`, but two `deploy_runtime.py` attempts timed out before confirming the
new agent startup/adoption heartbeat. The last heartbeat reported OBSERVE /
NEEDS_ATTENTION and held inputs false; the Control Plane now marks the runtime STALE.
A fresh read-only screenshot still showed alive Wilson, Day 37, and the active gift, and
the Xvfb/Steam/DST PIDs did not change. No click was sent.

No gift icon click has been sent because the live `GiftItemPopUp`/`Use Later` claim path
is not yet observable or implemented end to end. There is no real daily claim, durable
in-world claim, repeated gift claim, eligible-time measurement, or proof of AFK/activity
semantics. The session is online and the item server HealthCheck previously returned
OK, but authenticated reward eligibility is not established.

## Actual claim result — 2026-09-30, supersedes unclaimed checkpoint above

Path B retained the existing live session and all Xvfb/Steam/DST PIDs. Production
GameWorker performed bounded station approach, fresh ACTIVE detection, canonical
`CLICK_GIFT_ICON`, actual `GiftItemPopUp`, and canonical `CLICK_INWORLD_USE_LATER`.
Pinstripe Pants item `986745024922813965` received native
`SetItemOpened_Complete Success:200`, `Error=false`, Modified `1790780509.1803596`.
The backend ACK occurred at reveal, before Use Later closed the popup.

Close verification initially timed out. Recovery read the genuine production action
recording, sent no inputs, and required two fresh in-world observations before writing
an atomic, fsynced `IN_WORLD_GIFT_CONFIRMED` state. Source frame `r1-w1005-f2`,
canonical close `runtime-1:worker-1005:action-1`, final frame `r1-w1006-f2` at
2026-09-30T15:13:10.174148+00:00. Local before/received/after frames, action logs,
receipt, and stopped worker report are in `.data/claim-convergence-20260930/`.

The live in-world popup is separate from daily login. No eligible-start timestamp,
AFK/timer rule, weekly ordinal/target/reset, repeated claim, or daily claim is proven.
Control Plane heartbeat remains stale; agent PID 491 is intentionally suspended because
systemd control-group restart would also stop the preserved game processes.
