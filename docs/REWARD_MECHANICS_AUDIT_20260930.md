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

## Worker correction in this checkpoint

The worker now reports a visible gray toast as `IN_WORLD_GIFT_PENDING`, not
`NO_REWARD_AVAILABLE`, and exposes a separate `inworld_gift_state` telemetry field so
daily/login confirmation state is not inferred from the in-world icon. The first live
experiment sent canonical `Tab`/`OPEN_INVENTORY`; the gift stayed gray. Source review
showed that key opens `CONTROL_OPEN_INVENTORY`, not the crafting menu. A later
`OPEN_CRAFTING_MENU` action on `b` was transport and generic pixel-change verified,
but that did not prove the menu's state. `PlayerHud:OpenCrafting` calls
`GiftItemToast:ToggleController(true)`, hiding the toast; `CloseCrafting` shows it
again. At 14:14:49 UTC a fresh worker frame and read-only screenshot showed the toast
visible and gray, with `IN_WORLD_IDLE`, no held inputs, and no actionable gift. This
later frame does not say whether the crafting menu briefly opened after the action.

The generic `OPEN_CRAFTING_MENU` screen-change check was replaced with a fresh
verified-hidden `world_present_banner` postcondition and deployed. One live `b` press
then timed out after eight seconds with the gray toast still visible. GameWorker
returned to safe `OBSERVE`/`NEEDS_ATTENTION` with no held input. Repeating the
crafting-menu action is not useful. A single 0.45-second canonical step toward the
visible prepared Science Machine is now proposed when a pending gift is detected; it is
bounded to one attempt and has focused tests, but is not yet deployed.

The production gift click remains gated on an independently verified active icon; the
active icon reference is still unavailable, and the distinct in-world popup/result/claim
flow still needs captured live evidence and implementation. Daily login claim is also
not live-proven.

## Next evidence step

Deploy the one-shot canonical station-approach action and capture the immediate fresh
screen plus gift ROI. If the gift remains gray, the fixed prepared fixture is outside
the machine eligibility context; resolve its measured position/range before another
input. Only an active present sample can justify the gift click; first implement and
verify the distinct `GiftItemPopUp` flow. Do not treat the existing session duration as
eligible playtime, and do not infer a precise timer from community estimates.
