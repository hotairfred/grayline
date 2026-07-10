# Controlling a Consumer TV Rotator from Grayline (via a WiFi IR Blaster)

*A contributed how-to for driving a no-computer-interface TV antenna rotator from
Grayline's click-to-aim, by emulating `rotctld` over a $10 IR blaster. Written up by
**N8XS** for the RCA VH226E; the same recipe applies to any IR-remote TV rotator with
memory presets.*

> Pairs with Grayline's **`tv-30` rotator mode** — a rotator that only stops at fixed
> presets wants a compass-grid button layout instead of the default 0/45/90 quick-aim,
> and its 30° snapping is done in the bridge below (not in Grayline).

---

## The Goal

Click a DX spot in the Grayline cluster screen and have the antenna automatically point
the right direction — no separate rotator controller, no manual dialing.

## The Hardware

The rotator is an **RCA VH226E**, a consumer outdoor antenna rotator designed for TV
antennas — not a ham rotator. **No computer interface, no position feedback, no serial
port.** It's controlled entirely by a small hand-held IR remote, like a TV remote, and
can store **up to 12 memory presets** (press a preset button → it slews there and stops).

Grayline runs on a small Linux container on the home network. Clicking a bearing arrow
on a spot points the antenna at that station.

## The Key Insight

The rotator obeys IR commands from its remote. If a device could send the *same* IR
signals, software could control the rotator. A **Tuya Smart WiFi IR Blaster** does
exactly that — it sits near the control box, joins WiFi, and mimics any IR remote. The
Smart Life app "teaches" it each remote button, after which any button can be fired from
the app, a voice assistant, or a Python program.

## Step 1 — Program the rotator presets

12 memory presets at 30° intervals covering all directions (worst-case pointing error
±15°, fine for 6m where a Yagi's 3 dB beamwidth is 50–60°):

| Preset | Dir | Deg | | Preset | Dir | Deg |
|---|---|---|---|---|---|---|
| A | N   | 0   | | G | S   | 180 |
| B | NNE | 30  | | H | SSW | 210 |
| C | ENE | 60  | | I | WSW | 240 |
| D | E   | 90  | | J | W   | 270 |
| E | ESE | 120 | | U | WNW | 300 |
| F | SSE | 150 | | L | NNW | 330 |

## Step 2 — Teach the IR blaster

Place the Tuya blaster with line-of-sight to the control box's IR window. In Smart Life:
add the blaster, choose **DIY** remote type, then for each of the 12 preset buttons,
point the RCA remote at the blaster and press it so the app records the IR signal.

## Step 3 — Tuya developer account

To drive the blaster from Python (not the phone app), create a free Tuya IoT developer
account. **The one gotcha:** the developer project's **data center region must match the
region the Smart Life app was registered under** (here: Canada/US → *Western America*;
the first attempt used *Eastern America* and failed with a cryptic error). Enable the
handful of free API service modules that grant IR-command permission.

## Step 4 — The bridge (`tuya_rotctld.py`)

~200 lines of Python that act as glue between Grayline and the blaster:

1. **Listens** on a network port for rotation commands from Grayline (speaking the
   `rotctld` protocol Grayline already emits — so Grayline treats it like any rotor).
2. On a command (e.g. "point to 200°"), **finds the nearest 30° preset** (→ 210°,
   preset H / SSW).
3. **Sends** the command to the Tuya cloud → IR blaster fires that preset's signal.
4. The rotator moves. Full round-trip < 1 second. Runs as a boot-start system service.

## Step 5 — Connect Grayline

Two lines in Grayline's config point it at the bridge. After a restart, the bearing
buttons on the spot display are live.

## In Practice

A 6m spot appears in a needed grid → click the bearing arrow → within ~½ second the
control box beeps and the antenna slews to the nearest 30° preset. Typing a specific
bearing (200°) rounds to 210° and moves there.

## Accuracy & Limitations

- **Pointing ±15°** — inherent to 30° presets; plenty of margin against a 50–60° 6m
  beamwidth.
- **No position feedback** — the bridge tracks the last commanded position; if the
  physical remote is used in between, the displayed heading is stale until the next
  command.
- **Cloud dependency** — each command hops to Tuya's servers before the IR fires; if the
  internet is down, Grayline can't drive it (the physical remote still works).

## Considered but not done

- **Fine positioning** (VE3DCT technique): also teach the CW/CCW arrow buttons, time the
  slew rate, and combine a preset snap with timed arrow presses to hit arbitrary
  bearings (~±5°). Skipped — 30° is enough for current goals.
- **Arduino/ESP32 controller** replacing the rotator electronics for true continuous
  feedback. Evaluated, not pursued — the IR path meets the goal with zero hardware mods.

## Summary

A consumer TV rotator with no computer interface, fully integrated with Grayline's
click-to-aim via a ~$10 WiFi IR blaster and ~200 lines of Python — no hardware
modifications, essentially zero out-of-pocket cost. Click a DX spot, the beam points
itself.

---

*Station: N8XS, EM79si — July 2026. Contributed to the Grayline project as a reference
for anyone integrating an IR-remote TV rotator.*
