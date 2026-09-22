#!/usr/bin/env python3
"""
Regression test for the IKEA BILRESA E2490 scroll-wheel blueprint.

Drives the automation by publishing synthetic Zigbee2MQTT payloads through Home
Assistant's own mqtt.publish service, then asserts on the resulting entity state.
That isolates the blueprint's logic from the radio entirely - no need to touch the
physical remote, and every branch is reachable on demand.

Why this exists: the blueprint has failed three times in ways that are completely
silent in the UI (a template error in `variables:` aborting the run before any
condition, an empty `then:` block refused at load, and an `entity_id` validated at
blueprint-instantiation time). Traces show these, but a scripted pass/fail is faster.

CONFIGURATION - all via environment variables, nothing sensitive is stored here:

    HA_URL          default http://homeassistant.local:8123
    HA_TOKEN        a long-lived access token, OR
    HA_TOKEN_FILE   a file containing one (default ~/.config/ha/apartment.token)
    BILRESA_TOPIC   the remote's Zigbee2MQTT state topic
                    e.g. "zigbee2mqtt/My Remote"
    BILRESA_LIGHT   a light entity the blueprint controls
    BILRESA_HELPER  the input_select used for scroll modes (optional; the
                    mode-cycling test is skipped when unset)

PREREQUISITES for the relative-rotation tests:
  * The automation must be configured with Rotation Behaviour = relative.
  * "Simulated brightness" must be enabled on the device in Zigbee2MQTT (put any
    numbers in the Delta and Interval boxes) so that Zigbee2MQTT adds
    `action_brightness_delta` to rotation payloads.

USAGE:
    export BILRESA_TOPIC="zigbee2mqtt/My Remote"
    export BILRESA_LIGHT="light.my_lamp"
    export BILRESA_HELPER="input_select.my_scroll_mode"
    python3 test_bilresa_e2490_blueprint.py

Exits non-zero if any test fails.
"""

import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request

HA_URL = os.environ.get("HA_URL", "http://homeassistant.local:8123").rstrip("/")
TOPIC = os.environ.get("BILRESA_TOPIC", "")
LIGHT = os.environ.get("BILRESA_LIGHT", "")
HELPER = os.environ.get("BILRESA_HELPER", "")

# Blueprint defaults. Override if your automation differs.
MAX_DELTA = int(os.environ.get("BILRESA_MAX_DELTA", "60"))
RAIL_STEP = int(os.environ.get("BILRESA_RAIL_STEP", "10"))


def _token() -> str:
    tok = os.environ.get("HA_TOKEN")
    if tok:
        return tok.strip()
    path = pathlib.Path(
        os.environ.get("HA_TOKEN_FILE", "~/.config/ha/apartment.token")
    ).expanduser()
    if path.is_file():
        return path.read_text().strip()
    sys.exit("No token: set HA_TOKEN, or HA_TOKEN_FILE pointing at one.")


TOKEN = _token()


def api(path: str, data=None):
    req = urllib.request.Request(
        f"{HA_URL}/api{path}",
        data=json.dumps(data).encode() if data is not None else None,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
        },
        method="POST" if data is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read()
            return json.loads(body) if body else None
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code} on {path}: {e.read()[:200]!r}")


def state(entity):
    return api(f"/states/{entity}")


def service(domain, name, data):
    return api(f"/services/{domain}/{name}", data)


def publish(payload: dict):
    service("mqtt", "publish", {"topic": TOPIC, "payload": json.dumps(payload)})


def rotate(delta: int, level: int):
    """One scroll-wheel event, shaped exactly as Zigbee2MQTT emits it."""
    publish(
        {
            "action": "brightness_move_to_level",
            "action_brightness_delta": delta,
            "action_group": 21658,
            "action_level": level,
            "action_transition_time": 1,
            "battery": 100,
            "brightness": level,
            "linkquality": 200,
        }
    )


def attr(name):
    return state(LIGHT)["attributes"].get(name)


def settle(getter, timeout=8.0, stable_for=4, interval=0.25):
    """Poll until a value stops changing.

    Reading on the FIRST state change catches a mid-transition value and produces
    false failures - the blueprint applies a fade. Wait for stability instead.
    """
    last = getter()
    same = 0
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(interval)
        cur = getter()
        same = same + 1 if cur == last else 0
        last = cur
        if same >= stable_for:
            break
    return last


RESULTS = []


def check(name, got, lo, hi):
    ok = lo <= got <= hi
    RESULTS.append((ok, name))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name:38} got {got}  (expect {lo}..{hi})")


def set_mode(mode):
    if not HELPER:
        return False
    service("input_select", "select_option", {"entity_id": HELPER, "option": mode})
    time.sleep(0.5)
    return True


def expect_relative(start, delta):
    """Blueprint maths: effective delta is capped, then scaled over the 254 range."""
    eff = min(abs(delta), MAX_DELTA) * (1 if delta > 0 else -1)
    return round(start + eff / 255 * 254)


def main():
    missing = [n for n, v in (("BILRESA_TOPIC", TOPIC), ("BILRESA_LIGHT", LIGHT)) if not v]
    if missing:
        sys.exit(f"Set {', '.join(missing)} - see the docstring.")

    print(f"HA        : {HA_URL}")
    print(f"topic     : {TOPIC}")
    print(f"light     : {LIGHT}")
    print(f"helper    : {HELPER or '(none - mode tests skipped)'}\n")

    print("RELATIVE - brightness")
    set_mode("brightness")
    for delta, label in ((40, "step up"), (-40, "step down"), (MAX_DELTA + 40, "over the cap")):
        service("light", "turn_on", {"entity_id": LIGHT, "brightness": 100})
        start = settle(lambda: attr("brightness"))
        rotate(delta, 200)
        end = settle(lambda: attr("brightness"))
        want = expect_relative(start, delta)
        check(f"{label} (delta {delta:+})", end, want - 4, want + 4)

    print("\nRAILS - remote pinned at its limit, delta 0")
    service("light", "turn_on", {"entity_id": LIGHT, "brightness": 100})
    start = settle(lambda: attr("brightness"))
    rotate(0, 255)  # clamped at max: direction inferred from the level
    end = settle(lambda: attr("brightness"))
    want = round(start + RAIL_STEP / 255 * 254)
    check("rail step up", end, want - 4, want + 4)

    if HELPER:
        print("\nRELATIVE - colour temperature")
        set_mode("color_temp")
        service("light", "turn_on", {"entity_id": LIGHT, "color_temp_kelvin": 4000})
        start = settle(lambda: attr("color_temp_kelvin"))
        mn = attr("min_color_temp_kelvin") or 2000
        mx = attr("max_color_temp_kelvin") or 6500
        rotate(40, 200)
        end = settle(lambda: attr("color_temp_kelvin"))
        want = round(start + 40 / 255 * (mx - mn))
        check("4000K + delta 40", end, want - 80, want + 80)

        print("\nRELATIVE - hue")
        set_mode("hue")
        service("light", "turn_on", {"entity_id": LIGHT, "hs_color": [100, 80]})
        start = settle(lambda: (attr("hs_color") or [0, 0])[0])
        rotate(40, 200)
        end = settle(lambda: (attr("hs_color") or [0, 0])[0])
        want = round(start + 40 / 255 * 360)
        check("hue 100 + delta 40", round(end), want - 8, want + 8)

        print("\nTRIPLE CLICK cycles the scroll mode")
        before = state(HELPER)["state"]
        opts = state(HELPER)["attributes"]["options"]
        publish({"action": "off_double", "battery": 100, "linkquality": 180})
        after = settle(lambda: state(HELPER)["state"], timeout=6, stable_for=2)
        want_mode = opts[(opts.index(before) + 1) % len(opts)]
        ok = after == want_mode
        RESULTS.append((ok, "triple click cycles mode"))
        print(f"  [{'PASS' if ok else 'FAIL'}] triple click: {before} -> {after} (expect {want_mode})")
        print("\n  NOTE: requires 'Triple Click Cycles Mode' enabled on the automation.")

    passed = sum(1 for ok, _ in RESULTS if ok)
    print(f"\n=== {passed}/{len(RESULTS)} passed ===")
    if passed != len(RESULTS):
        print("Failures:", ", ".join(n for ok, n in RESULTS if not ok))
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
