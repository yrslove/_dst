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

## Worker correction in this checkpoint

The worker now reports a visible gray toast as `IN_WORLD_GIFT_PENDING`, not
`NO_REWARD_AVAILABLE`, and exposes a separate `inworld_gift_state` telemetry field so
daily/login confirmation state is not inferred from the in-world icon. When this
pending state is seen in ACTIVE mode, the worker sends one bounded canonical
`OPEN_INVENTORY` action to enable the prepared nearby giftmachine. The action must
verify a fresh visible in-world screen change. Gift clicking remains gated on an
independently verified active icon; the actual in-world popup/result/claim path still
needs separate implementation and live proof.

## Next experiment

Continue the existing account/world/session. Use the canonical `OPEN_INVENTORY`
action when the retained gray toast is present. Capture fresh full-frame and gift-ROI
evidence, record whether the toast becomes enabled, and then implement/verify the
separate `GiftItemPopUp` flow before allowing any gift click. Only afterward compare
controlled safe activity with idle time; do not restart the current eligibility
session or infer a precise drop timer from the community estimate.
