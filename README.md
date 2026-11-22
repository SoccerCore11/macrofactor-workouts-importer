# MacroFactor workout importer (ADB / uiautomator2)

> **Experimental. No support, no warranty.** I wrote this for a one-off migration of my own history.
> It worked for me; it may not work for you. Read the whole README before running it. You use it at
> your own risk (see [LICENSE](LICENSE)).

MacroFactor Workouts has no import feature, API, or MCP server. This project enters past workouts
through the Android app's own UI. It drives the screen over ADB (with
[uiautomator2](https://github.com/openatx/uiautomator2) for fast UI dumps). Input is the CSV export
from **Strong** and/or **Progression**. Each workout is entered as a normal finished workout with its
original date, time, duration, exercises, set order, set types, loads, reps, time, distance, notes,
and RIR (converted from RPE). Each one is then reopened and checked against the plan.

[scrcpy](https://github.com/Genymobile/scrcpy) is handy for watching it run. It also bundles `adb`.

## Built for one device

All screen coordinates are hard-coded for a **Google Pixel Tablet, 2560×1600, landscape**. They
target the MacroFactor Workouts version available in early 2026. Other tablets, phones, orientations,
display sizes, font scales, or app versions **will** need changes. Look at `mf.tap(x, y)` calls and
the `n.c[0]`/`n.c[1]` position filters in `importer.py`. Most navigation uses text labels, but
keypads, menus and table columns use positions. Watch the first few workouts closely.

## Read this first

- **Terms of service.** Automating the app's UI may be against MacroFactor's terms. Check them yourself.
- **It changes the device clock.** The workout Start Time picker only goes back about 30 days.
  So for each workout the importer turns off automatic time (`settings put global auto_time 0`) and
  sets the device clock to that workout's date. Other apps on the device will see a wrong date during
  the run. The importer restores automatic time at each month change and whenever `run` exits. If
  the process is killed, run `python mf.py clock auto` yourself.
- **Sync can silently fail.** In one run, about 300 workouts saved under a backdated clock were
  rejected by the server (`PERMISSION_DENIED` in logcat). The app then quietly removed them from the
  device. Now, at each month change, the importer returns the clock to real time and waits for the
  upload. If logcat shows a rejected write, it stops. **Check another device** (e.g. your phone)
  afterwards to make sure old workouts actually synced.
- **App settings.** During the run, set Settings ▸ Workouts ▸ *Warm-Up Automation* to **None**, and
  turn **off** *Propagate Changes* and *Use Rest Timers*. Note your original values first and restore
  them afterwards.
- **Keep the screen awake.** The device locked mid-run once.
  `adb shell settings put global stay_on_while_plugged_in 7` keeps it on while charging. Set it back
  to `0` when done.
- **You need an anchor workout.** At least one workout must already exist in MacroFactor that is
  *newer* than everything you import. The importer finds its entries just below that workout in
  History. Put that workout's date, exactly as History shows it (e.g. `"Jan 1, 2026"`), in
  `history_anchor`.

## Requirements

- Python 3.10+
- `pip install -r requirements.txt`
- `adb` on `PATH` (or set `MF_ADB` to its full path), USB debugging on, device authorised
- MacroFactor Workouts installed and signed in, on the Workout tab

## Workflow

All working files go in `./data/`, or in `MF_DATA_DIR` if set. This folder is git-ignored. It holds
your personal training history.

1. **Prepare.** Parse the exports into `data/manifest.json`, the ledger `data/progress.sqlite3`, and a
   blank `data/mapping.json`:

   ```sh
   python prepare_workouts.py --strong strong.csv --progression progression.csv
   ```

   Also look at `data/timestamp-review.json`. It lists workouts whose times look wrong (for example, a
   timer left running). Fix those with overrides (below).

2. **Config.** Copy `examples/config.example.json` to `data/config.json` and edit it (reference below).

3. **Catalog.** Open the exercise chooser in the app with no filters, then run:

   ```sh
   python scrape_catalog.py
   ```

   This writes `data/catalog.json`.

4. **Map exercises.** Fill in exact name matches automatically:

   ```sh
   python fill_mapping.py
   ```

   Then set `target` by hand for the rest. Choose carefully: keep variants distinct (incline vs flat,
   barbell vs dumbbell, chin-up vs pull-up, assisted vs not). When the match is approximate, the
   original name goes into the exercise note.

5. **Dry run.** No device needed:

   ```sh
   python importer.py plan <workout_id>
   ```

   This prints exactly what will be entered. `python preflight.py` plans every pending workout and
   summarises the targets and any errors.

6. **Import.**
   - `python importer.py run 1` imports one workout. Watch it.
   - Then try bigger batches, e.g. `python importer.py run 50`.
   - Oldest workouts go first.
   - Create `data/STOP` to halt cleanly before the next workout.

7. **Check.** `python scan_history.py` scrolls the whole History and compares per-date counts with
   the source. Then confirm a few old dates on another device.

8. **Clean up.**
   - `python mf.py clock auto`
   - Restore the app settings.
   - Turn off stay-awake.

### Ledger

`data/progress.sqlite3` tracks each workout's status: `pending`, `entering`, `saved_unverified`,
`saved_mismatch`, `verified`, or `sync_rejected`. A run stops at the first mismatch or surprise in the
UI. Rerunning `run` picks up where it left off. If a workout stopped part-way through entry, it is
detected as a duplicate rather than entered twice. `python importer.py verify <id>` re-checks a saved
workout.

## Conventions and decisions

These are choices I made for my data. Check that they fit yours.

- **Units.** The importer enters loads in **lb**, so set the app to pounds. kg loads are converted
  with 2.20462262185. The result is snapped to a whole, half or quarter pound only when that is within
  the precision the source recorded (so 45.359237 kg becomes 100 lb); otherwise it keeps 2 decimals.
  The logged load is entered as-is: no bar weight is added, and dumbbell weights are neither doubled
  nor halved.
- **Set types.** Strong `W` becomes a warm-up and `F` becomes a failure set. A warm-up logged *after*
  working sets is treated as a logging slip and moved first.
- **RPE → RIR.** MacroFactor records RIR. The `rpe_to_rir` table maps RPE values to RIR. An RPE that
  is not in the table stops the run until you add a rule for it. RPEs listed in `rpe_note_for` are
  also written into the set note, because the RIR loses detail for them.
- **Notes.** Strong "Note" rows and Progression set/session comments become exercise notes. Session
  comments get `session_note_prefix` and go on the first exercise. Notes longer than the app's
  300-character limit are rejected.
- **Bodyweight exercises.**
  - `zero_load_is_bodyweight`: a 0 load means plain bodyweight, so no load is entered.
  - `mode: "bw_range"` is for sources that log *total* load, bodyweight included. Loads between
    `bw_low` and `bw_high` count as bodyweight. Loads below `bw_low` count as a machine-assisted
    "felt" load. Those go to `assisted_target` with assistance = the app's bodyweight share − felt load.
  - The app's bodyweight share for each exercise is learned into `data/bw-parts.json`. Until then it
    falls back to `app_bw`.
- **Distance.** km values are converted to the app's unit with `km_to_mi`. If the target needs a
  distance and the source only has time, `est_pace_min_per_km` estimates one and notes it.

### `data/config.json`

| key | meaning |
| --- | --- |
| `history_anchor` | Date of the oldest workout already in MacroFactor, as History shows it, e.g. `"Jan 1, 2026"`. Required. |
| `rpe_to_rir` | RPE → RIR table (string keys). |
| `rpe_note_for` | RPE values also copied into the set note. |
| `distance_unit`, `km_to_mi` | Distance unit used by the app and the km conversion factor. |
| `session_note_prefix` | Prefix for session comments. |
| `felt_note` | Note template for assisted felt loads. |
| `app_bw` | Fallback bodyweight share (lb) for assisted exercises. Use what the app shows for *you*. |
| `workout_tab_regex` | Optional extra regex used to recognise the Workout tab (e.g. your active program's name). |

### `data/mapping.json`

Keys are source exercise names. Required fields: `target` (exact catalog name), `confidence`, and
`sources`. Optional fields: `mode`/`assisted_target`/`bw_low`/`bw_high`, `zero_load_is_bodyweight`,
`loadless_target` (reps with no load go to this target instead), and `est_pace_min_per_km`. See
`examples/mapping.example.json`. Its values are illustrative only.

### `data/overrides.json` (optional)

Corrections for individual workouts, keyed by workout id:

- `{target: {field: value}}` replaces a field on every set of that exercise.
- `"_session": {"start", "dur", "why"}` replaces the workout's start time and duration.

See `examples/overrides.example.json`.

## Other tools

- `mf.py`: a small ADB driver with a command-line interface for exploring the UI:
  - `python mf.py dump [regex]`
  - `python mf.py tapt REGEX`
  - `python mf.py clock "YYYY-MM-DD HH:MM"`
- `probe_cols.py`: reads the column headers for new targets in a throwaway draft workout.

### Environment variables

- `MF_ADB`: path to `adb`.
- `MF_DATA_DIR`: the data folder.
- `MF_PAUSE_SCALE`: multiplies every UI pause. The default is `0.5`; raise it on a slower device.
- `MF_U2=0`: turns off uiautomator2 and uses plain `adb` (slower).

## Not affiliated

This is not affiliated with or endorsed by MacroFactor, Strong, or Progression. All trademarks belong
to their owners.
