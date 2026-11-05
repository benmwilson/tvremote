# UI revamp: TV-first remote and share picker

Status: draft for Ben's approval. Nothing is built from this until he says yes.
Branch: `ui-revamp` (worktree at `.worktrees/ui-revamp`). `main` stays deployable.

## Why

This project exists because casting a stream straight to a TV buffers and lags. The Pi-on-HDMI path fixed that for the living room. Ben now has two TVs (living room: Pi on HDMI; bedroom: Chromecast) and wants to share a link from his iPhone, choose a TV, and have it play, smoothly.

The revamp is about that loop: **share, pick a TV, watch without buffering**, and being able to see what each TV is doing.

## Goals

1. **Share → pick → playing.** From the iPhone share sheet, one Shortcut opens a picker; one tap on a TV card starts playback there and shows that TV's remote. Two taps after Share, counting the card.
2. **Two views, swappable.** *All TVs*: a card per TV. *One TV*: the full remote for a single TV. Tapping a card opens that TV's remote; a control in the header goes back.
3. **TV cards** show: name, brand + screen size + model (e.g. "TCL 43″ · 43S425-CA"), what is playing now (title, source badge Kick/Twitch/YouTube/other, LIVE or elapsed time, paused), power state, current input, and the connection type ("Pi on HDMI" / "Chromecast").
4. **Playback health is visible.** Each TV shows whether the stream is smooth or buffering, how long it took to start, and on the Pi the dropped-frame count. The existing debug drawer keeps the detail. (This item is my addition, tied to Ben's reason for the project; he has not yet confirmed it.)
5. **The picker prevents wrong-TV plays.** It always asks. The TV used last is listed first. A TV that is already playing something is labelled "Replaces: <title>" so you can see what you're about to cut off. (Wording and the "replaces" label are my call.)
6. Phone-first at 390 px wide, large touch targets, dark, Canadian spelling, works as an iOS Home Screen web app. Keep it one dependency-free HTML file.

## Non-goals

- A Web Share Target. iOS Safari does not support it, so the Shortcut stays the way in.
- New frameworks, build steps or libraries.
- The chat overlay for the Chromecast, the multi-stream grid and the setup wizard. They are separate projects (see the roadmap in `CLAUDE.md`). The design should not make them harder: TVs are rendered from a list the server returns, not written into the page.
- Changing how playback works or the existing `/play` and `/api/*` contracts.

## Anti-goals (what makes this a failure even if it technically works)

- **Lag or buffering.** The UI must never be the reason a stream is slower, and must never hide a buffering stream. If something buffers, the card says so.
- **Playing on the wrong TV.** A shared link must never start on a TV the user didn't pick.
- **A slow picker.** The picker must be usable immediately. It renders from a cached list; per-TV details (power, input, what's playing) fill in as they arrive, and one unreachable TV must not hold up the others (timeouts of about 2 s per TV, shown as "can't reach").
- **Extra taps.** No confirm dialogs on the normal path.

## Constraints

- Existing API stays: `/play`, `/api/play`, `/api/cmd`, `/api/status`, `/api/targets`, `/api/roku`, `/api/tv`, `/api/debug` and the `?target=` parameter.
- TVs are `living` (Pi on HDMI, mpv) and `bedroom` (Chromecast). Targets stay hard-coded server-side until the wizard project.
- Password support stays: Basic auth for the UI and `&t=` for `/play`. The picker is served from the same origin, so API calls from it carry the browser's credentials.
- No real addresses or device names in the repo. Test data uses placeholders.
- The Pi is not reachable from Claude Code except over HTTP; Ben runs Pi-side commands.

## Decided with Ben

- Both layouts exist, with a swap between them.
- The picker shows every time you share. The Shortcut can still add `&target=<id>` to skip the picker (for a "Play in Bedroom" Shortcut).
- What a TV card tells you: name, brand, size/model, what's playing. He suggested resolution too, but the TVs do not report it, so the card shows the **stream's** resolution (e.g. 1080p60) instead. That substitution is my call.

## How it works

**The Shortcut (one action).** Receive input from the Share Sheet (fallback: Get Clipboard) → URL Encode → Open URL `http://<pi>:8765/share?u=<encoded>`. For the Kick app, which hides Shortcuts from its share sheet, the flow stays Copy → run Shortcut.

**`/share` page.** Same HTML file, shows the picker. Tapping a card sends `POST /api/play {url, target}`, then switches to that TV's remote. If the link can't be parsed, the page says so and offers to paste a link.

**Server additions (small).**
- `/api/targets` returns, for each TV, the cached details: name, brand, model, screen size, connection type, online, power, input, and now-playing (title, source, live, paused, health). It answers from a cache refreshed in the background (every few seconds while a page is open) so it never waits on a TV.
- Health for the Chromecast comes from `player_state` (`BUFFERING` vs `PLAYING`) and the start time; for the Pi from `frame-drop-count` and mpv's `paused-for-cache`.
- `GET /share` serves `index.html`.

**Views.** *All TVs* is the home screen. *One TV* is today's remote reorganised (now-playing hero, big play/pause, volume with the number, seek, quality, favourites, TV card with power/volume/input, Play box, debug drawer), reached by tapping a card. State of the selected TV is remembered.

## Left to the builder

- Visual design: palette, type, layout details, motion. Use the frontend-design and ui-ux-pro-max guidance; avoid generic dark-mode-plus-green-accent defaults and reuse of one card shape for everything.
- UX copy for empty, error and busy states (ux-copy skill). Errors say what happened and how to fix it ("Bedroom isn't reachable. Check the Chromecast is powered and on the same Wi-Fi.").
- Accessibility: contrast, focus, labels, touch targets (accessibility-review skill). Respect reduced motion.
- Test with a stub API server at 390 px and desktop width in Chrome; then on the Pi with Ben.

## Verification

- Stub-server render checks at 390 px for: All TVs, One TV, picker, picker with an unreachable TV, picker with a busy TV, empty states, debug drawer.
- A picker-speed check: with one TV artificially slow (5 s), the picker is interactive in under 1 s and the other TV's card is complete.
- On the Pi (Ben): share a Kick link, pick each TV, confirm it plays on that TV only; confirm the card shows buffering when the stream is interrupted.
- Review passes before merge: design critique, accessibility review, code review, security review of the new routes.

## Open questions for Ben

- Is the "Replaces: <title>" label on busy TVs what you want, or should the picker warn some other way?
- Should the health indicator be on the card, or only in the debug drawer?
