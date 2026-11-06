# TV Registry, Stage 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** TVs become the saved, named thing (each with what is plugged in: the Pi on an HDMI input, a Chromecast), replacing the two fixed slots, with a rewritten setup screen.

**Architecture:** A new `/data/tvs.json` holds a list of TVs. `tvconfig` validates it, migrates the old `targets.json`, and *derives* the settings the app already uses (`ROKU_IPS`, `INPUTS`, `LABELS`, `TV_ENABLED`, the Chromecast device) for the two existing playback endpoints `living` (Pi) and `bedroom` (Chromecast). Nothing downstream of `apply_tv_config` changes. The wizard step is rewritten around TV cards.

**Tech Stack:** Flask (`app/app.py`), plain Python modules, one dependency-free `app/index.html`, `unittest` (`python -m unittest discover -s tests`).

**Spec:** `docs/superpowers/specs/2026-11-05-tv-registry-design.md`

## Global Constraints

- Keep endpoint ids `living` (Pi, mpv) and `bedroom` (Chromecast) internal; the UI shows only names. Do not rename them in stage 1.
- At most one TV has `pi`, at most one has `chromecast`. Names 1-24 chars, unique (case-insensitive). `input` is one of `"", HDMI1..HDMI4`.
- `roku` must pass `tvconfig.lan_address` (private IPv4) or be empty.
- Admin only for every endpoint here (`admin_only()`); guests never.
- Never delete `/data/targets.json`. A migration that fails leaves the app on the old config.
- Saving must not touch what a TV is playing. Scanning never saves.
- Canadian spelling; plain words; no "slot". Single-file UI, no new dependencies, no horizontal scroll, confirm before anything destructive.
- Run `python -m pyflakes app/*.py` and the full suite before each commit; both must be clean/OK.

## Review Focus

- A fresh install (no `tvs.json`, no `targets.json`): the wizard works and the app starts with no TVs.
- `targets.json` where both slots name the *same* Roku (the real current state after the Pi moved): one TV carries both, labels get ` · Pi` / ` · Chromecast`.
- A Roku saved earlier that the scan no longer finds: the card stays, marked "didn't answer", never silently dropped.
- Ticking the Pi on a second TV: it leaves the first (and says so); saving two `pi` TVs through the API is refused.
- A Chromecast named in the config that isn't found now: kept as "(saved, not found now)", not erased.

---

### Task 1: TV list model, validation, derivation, migration

**Files:**
- Modify: `app/tvconfig.py`
- Test: `tests/test_tvconfig.py` (create)

**Interfaces:**
- Produces:
  - `load_tvs(data_dir) -> list[dict]`: reads `tvs.json`; if missing, returns `migrate(load(data_dir))`; `[]` when neither exists.
  - `clean_tvs(raw: dict) -> list[dict]`: validates `{"tvs":[...]}`, raises `ValueError` with a plain sentence; returns normalized TVs `{"id","name","roku","pi": {"input"}|None,"chromecast": {"device","input"}|None}`, assigning `tv<n>` ids to new ones.
  - `save_tvs(data_dir, tvs) -> list[dict]`: writes `{"version":1,"tvs":tvs}` through `store.write_json`.
  - `migrate(old: dict) -> list[dict]`: old `{"living":{...},"bedroom":{...}}` → TVs (rules in the spec).
  - `endpoints(tvs) -> dict`: `{"living": {"roku","input","label","enabled"}, "bedroom": {"roku","input","label","enabled","device"}}`; label gets ` · Pi` / ` · Chromecast` suffix only when one TV carries both; an endpoint with no TV has `enabled False`, empty strings, label `DEFAULT_NAMES[id]`.
- Keeps `lan_address`, `INPUTS`, `IDS`, `DEFAULT_NAMES`, and the old `load/clean/save` until Task 3 removes their last callers.

- [ ] **Step 1: Write failing tests** in `tests/test_tvconfig.py`: `test_migrate_two_slots_with_different_rokus_makes_two_tvs`, `test_migrate_both_slots_on_one_roku_makes_one_tv_holding_both` (living `.141 HDMI1` + bedroom `.141 HDMI1 device "Bedroom TV"` → one TV, `pi.input == "HDMI1"`, `chromecast.device == "Bedroom TV"`), `test_migrate_skips_disabled_slots`, `test_clean_refuses_two_pis_two_chromecasts_duplicate_names_bad_ip_bad_input_and_long_names_are_trimmed`, `test_endpoints_suffixes_labels_only_when_one_tv_has_both`, `test_endpoints_disables_an_endpoint_with_no_tv`, `test_load_tvs_migrates_when_tvs_json_is_missing_and_never_touches_targets_json`, `test_load_tvs_on_a_fresh_install_is_empty`.
- [ ] **Step 2: Run** `python -m unittest tests.test_tvconfig`; expect failures (names undefined).
- [ ] **Step 3: Implement** the five functions above in `app/tvconfig.py`. Migration of the real current file (`living` `.141` HDMI1; `bedroom` `.142` HDMI1, device "Bedroom TV") yields two TVs: "Living room" with `pi`, "Bedroom" with `chromecast`.
- [ ] **Step 4: Run** the new tests plus the full suite; expect OK.
- [ ] **Step 5: Commit** `git commit -m "TV list model: validation, migration from the two slots, derived endpoint settings"`.

### Task 2: Suggestions from the scan

**Files:**
- Modify: `app/discover.py`
- Test: `tests/test_discover.py`

**Interfaces:**
- Consumes: Task 1 TV dicts.
- Produces: `suggest(rokus, casts, display, cec_name, pi_input, saved_tvs) -> list[dict]`: one entry per Roku found plus one per saved TV not found, each `{"id","name","roku","found": bool,"pi": {"input"}|None,"chromecast": {...}|None,"why": [str], "pi_detected": str}`. Saved values win. For Rokus not saved: the Pi is ticked on the one whose model matches `cec_name` (else on the only Roku if exactly one); the Chromecast on the same TV when there is exactly one Chromecast and one Roku, else left off with a "why" line. `pi_detected` is `pi_input` on the TV the Pi is ticked on, else `""`. Replaces `guess`; remove `guess` and its tests once `suggest` covers the same cases.

- [ ] **Step 1: Write failing tests**: `test_suggest_ticks_the_pi_on_the_roku_whose_model_matches_the_cec_name`, `test_suggest_prefers_saved_values_and_flags_a_changed_hdmi_port`, `test_suggest_keeps_a_saved_tv_the_scan_missed_marked_not_found`, `test_suggest_has_no_pi_when_no_screen_is_attached`, `test_suggest_with_two_rokus_and_no_cec_name_guesses_nothing_and_says_why`.
- [ ] **Step 2: Run** them; expect failures.
- [ ] **Step 3: Implement** `suggest` in `app/discover.py`; delete `guess` and its old tests.
- [ ] **Step 4: Run** the full suite; OK.
- [ ] **Step 5: Commit** `git commit -m "Scan suggestions per TV instead of per slot"`.

### Task 3: Backend wiring and endpoints

**Files:**
- Modify: `app/app.py` (`apply_tv_config`, `/api/discover`, `/api/targets/config`, startup call near `apply_tv_config(tvconfig.load(DATA))`)
- Test: `tests/test_discover.py` (Endpoints class), `tests/test_api.py` if it touches the old config

**Interfaces:**
- Consumes: Task 1 `load_tvs/clean_tvs/save_tvs/endpoints`, Task 2 `suggest`.
- Produces:
  - `apply_tv_config(tvs: list[dict]) -> None` (new meaning: takes the TV list, calls `tvconfig.endpoints`, fills `ROKU_IPS/INPUTS/TV_ENABLED/LABELS`, updates `CAST.cfg`/`CAST.name`, clears `_roku/_apps/_icons` as today).
  - `GET /api/discover` → `{rokus, chromecasts, display, cec_name, pi_input, tvs: suggest(...), saved: load_tvs()}`.
  - `GET /api/tvs` → `{"tvs": load_tvs(DATA)}` (admin).
  - `POST /api/tvs` body `{"tvs": [...]}` → `clean_tvs`, `save_tvs`, `apply_tv_config`; `400 {"ok": false, "error": sentence}` on `ValueError`. Remove `/api/targets/config`.
  - Startup: `apply_tv_config(tvconfig.load_tvs(DATA))`; if migration happened, write `tvs.json` once.

- [ ] **Step 1: Update tests**: replace the old `/api/targets/config` tests with `test_saving_tvs_applies_endpoints_at_once` (post two TVs, assert `server.ROKU_IPS`, `INPUTS`, `LABELS`, `TV_ENABLED`), `test_two_pis_are_refused_with_a_sentence`, `test_tvs_endpoints_are_admin_only`, `test_discover_returns_per_tv_suggestions`, `test_startup_migrates_the_old_file_once_and_leaves_it_in_place`.
- [ ] **Step 2: Run** them; expect failures.
- [ ] **Step 3: Implement** the changes in `app/app.py`; keep `/api/targets/test` as is; update the `tvconfig.IDS` use at the move endpoint only if it breaks.
- [ ] **Step 4: Run** pyflakes and the full suite; OK.
- [ ] **Step 5: Commit** `git commit -m "Save and apply TVs from tvs.json; per-TV discovery"`.

### Task 4: The new setup screen

**Files:**
- Modify: `app/index.html` (the `setup2` markup, `discoverTvs`, `tvSlot`, `renderTvSlots`, `tvsave` handler, summary on step 3, the Settings "TVs" section text)
- Test: stub-based manual check in Chrome (below); `tests/test_setup.py` for anything the wizard markup must keep (guest cannot see it).

**Interfaces:**
- Consumes: `GET /api/discover`, `POST /api/tvs`, `POST /api/targets/test` (unchanged).
- Produces: one card per entry of `tvs`: Name input; "What's plugged in?" with two switch rows (This Pi + input select with "we detected HDMI N" and a one-tap "Use HDMI N"; Chromecast + device select + input select); "Test this TV"; the "nothing plugged in" note; "didn't answer" help for `found: false`; footer buttons Cancel (re-run only), Scan again, Save. Ticking Pi/Chromecast on one TV unticks it on the other and shows a one-line note. Scan again keeps unsaved edits for TVs still present.
- Copy (verbatim): title "Your TVs"; intro "We found N TV(s), N Chromecast(s) and a screen on the Pi."; section "What's plugged in?"; Pi row "This Pi"; empty note "Nothing plugged in. You can still turn it on and change volume or input from the remote."; missing TV "Didn't answer. On that TV: Settings, System, Advanced system settings, Control by mobile apps, Network access, Permissive."; buttons "Cancel", "Scan again", "Save".

- [ ] **Step 1: Build the markup and script** per the Interfaces; apply the `frontend-design`/`ui-ux-pro-max` guidance for spacing, 44px touch targets and the existing `.wz-*` styles.
- [ ] **Step 2: Check on the stub** (`python tests/stub_server.py 8799`, mock `/api/discover` and `/api/tvs` in the page with the real current values: two Rokus, one Chromecast, `pi_input: HDMI3`, saved old pairing) in Chrome: cards render, the HDMI 3 hint and "Use HDMI 3", ticking the Pi on the other TV moves it, Cancel posts nothing, Save posts the expected body, no horizontal scroll at 390 px, step 3 summary lists the saved TVs.
- [ ] **Step 3: Run** the full suite; OK.
- [ ] **Step 4: Commit** `git commit -m "New setup screen: a card per TV with what is plugged in"`.

### Task 5: Deploy, verify on the Pi, update notes

**Files:**
- Modify: `CLAUDE.md` (Todo: stage 2 items; layout notes), `docs/multi-view.md` untouched

- [ ] **Step 1: Push** and, with Ben's go-ahead, rebuild the Pi over SSH when nothing is playing (`docker exec tvremote curl -s http://127.0.0.1:8765/api/status` shows `running:false`).
- [ ] **Step 2: Verify**: `ssh ... "docker exec tvremote cat /data/tvs.json"` shows the migrated TVs and `targets.json` is still there; in Chrome the wizard shows the real Rokus, the Chromecast and "detected HDMI 3"; saving from the wizard changes the card names/labels in All TVs.
- [ ] **Step 3: Update** the `CLAUDE.md` Todo (stage 2: card per TV, TV-only cards, inline rename) and commit `git commit -m "Notes: TV registry stage 1 done, stage 2 todo"`.
