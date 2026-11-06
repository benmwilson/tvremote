# TVs first: the TV registry and a new setup screen

Date: 2026-11-05. Status: approved by Ben in chat ("real fix and improve, maybe revamp entirely").

## Why

The app and its setup wizard are built around two fixed slots: `living` (the Pi's own screen, mpv) and `bedroom` (a Chromecast). Each slot carries
one Roku. That broke the day the Pi moved to the bedroom TV: the Pi and the Chromecast are now on the *same* TV, the living-room Roku has
nothing plugged in, and the wizard can't say either ("The Pi's own screen" labelled "Living room" is simply wrong). Ben wants to swap the
Pi to any TV, or a new one, from the admin panel, with a setup screen that is plain to read.

## Goals

- A TV is a first-class thing: found by the scan (a Roku), named by the owner, saved in `/data`.
- Under each TV the admin says *what is plugged into it*: **the Pi** (on a chosen HDMI input) and/or **a Chromecast** (one of the discovered
  ones, and its input). Either may be absent. Nothing is hard-coded to "living" or "bedroom".
- A TV with nothing plugged in still gets a card and the Roku controls (power, volume, input, apps, keys). It can't play links.
- Moving the Pi = untick it on one TV, tick it on another (the wizard pre-fills the HDMI input from the Pi's real CEC address).
- The setup screen is rewritten in plain words; one card per TV; changes only apply on Save; Cancel discards.
- Existing installs keep working: the old `targets.json` is converted on first start.

## Non-goals and anti-goals

- No second Pi, no second Chromecast target (the app drives one Pi screen and one Chromecast; more is a later step).
- No AirPlay, no non-Roku TV control (CEC power for non-Roku is out of scope).
- Must not lose a working setup: a failed migration leaves the old file untouched and the app on the old behaviour.
- Must not change what any TV is playing when settings are saved.

## Constraints

- `app.py` runs every target through the ids `living` (Pi, mpv) and `bedroom` (Chromecast): about 50 uses plus the UI and tests.
  Stage 1 keeps those two ids as the names of the two *playback endpoints* (internal), and derives everything about them (Roku address,
  input, label, enabled) from the TV list. Nothing else is renamed in stage 1. Stage 2 introduces id-per-TV in the UI.
- Single-file dependency-free UI, Canadian spelling, large touch targets, no horizontal scroll, icons on buttons, confirm before destructive acts.
- Guests never see or change this. Owners and admins both can.

## The data

`/data/tvs.json` (new; `targets.json` is left in place as a backup):

```
{"version": 1,
 "tvs": [
   {"id": "tv1", "name": "Bedroom", "roku": "192.168.1.142",
    "pi": {"input": "HDMI3"},                       # or null
    "chromecast": {"device": "Bedroom TV", "input": "HDMI1"}},   # or null
   {"id": "tv2", "name": "Living room", "roku": "192.168.1.141", "pi": null, "chromecast": null}]}
```

Rules (enforced in one `tvconfig.clean`): names 1-24 chars, unique; `roku` is a private LAN IPv4 or empty (a TV with no Roku can still hold a
Chromecast); `input` is `""` or HDMI1-4; at most one TV has `pi`, at most one has `chromecast`; ids are `tv` + a number.

Derived at startup and on save (`apply_tv_config`): the `living` endpoint takes the Roku, input and label of the TV that has `pi`; the
`bedroom` endpoint those of the TV that has `chromecast`; an endpoint with no TV is disabled (as `enabled: false` today). When one TV carries
both, their labels become `<name> · Pi` and `<name> · Chromecast` so the two cards can't be confused.

Migration (`tvconfig.migrate`): from `targets.json` slots. `living` → a TV holding the Pi (name, roku, input); `bedroom` → a TV holding the
Chromecast. When both name the same Roku they merge into one TV. A disabled slot contributes nothing. Rokus found by the scan but absent from
the config appear as new, unticked TVs in the wizard (not saved until the admin saves).

## The setup screen ("Your TVs")

One card per TV (every Roku the scan found, plus any saved TV not found now, marked "didn't answer"):

- Header: size and brand, the Roku's own name, address. A TV that didn't answer says how to fix it (Control by mobile apps: Permissive).
- **Name** field (prefilled from what was saved, else a suggestion from the Roku's name).
- **What's plugged in?** Two rows with switches:
  - *This Pi* → shows the HDMI input select. Pre-filled from the Pi's real CEC address and marked "we detected this"; if it differs
    from what was saved, a one-tap "Use HDMI N". Off and disabled with a reason when the Pi has no screen attached.
  - *Chromecast* → a select of the discovered Chromecasts and the input. Off with a reason when none was found.
  - Ticking the Pi or Chromecast on one TV unticks it elsewhere (one of each), saying so.
- "Test this TV": Info key to the Roku (unchanged) and, when the Pi is ticked, nothing else (no playback is started).
- Nothing plugged in shows: "You can still turn it on and change volume or input from the remote."
- Footer: Cancel (re-runs only), Scan again, Save. First-time setup shows Save only. Unsaved changes are kept when scanning again.
- Wording: no "slots", "Its Roku TV", "HDMI input to switch to when you play"; see the mock in the conversation.

Admin Settings "TVs" section lists the saved TVs (name, what's plugged in, input) with "Change or move a TV" opening the same screen.

## Stage 2 (after stage 1 is on the Pi)

- A card per TV in All TVs, with a "Play on" switch when a TV has both the Pi and a Chromecast.
- TV-only cards (no playback) in All TVs and the Play picker (hidden there), with the Roku remote working.
- Rename a TV or change its input without the wizard (inline, admins only).

## Decided by me (check these)

- Keep the internal ids `living`/`bedroom` through stage 1 (lowest risk; the UI shows only names).
- One Pi and one Chromecast at most, as today.
- The scan never saves anything; only Save does. The old `targets.json` is never deleted.
- A TV with no Roku can exist only to hold a Chromecast (so a Chromecast on a non-Roku TV still works).

## Left to the builder

CSS details, exact copy within the tone above, the test layout, whether `tvconfig` or a new module holds the migration.
