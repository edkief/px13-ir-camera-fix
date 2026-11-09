#!/usr/bin/env python3
"""
Measure how many illuminated frames the IR camera delivers before the firmware
emitter cut-off, under scenarios you set up by hand.

    python3 tools/cutoff_test.py [runs-per-scenario]

For each scenario, set the scene, press Enter, and hold still for ~15 s.
Results are appended to cutoff_results.csv next to this script.
"""
import csv, os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import msxu_reader as m

SCENARIOS = [
    ("absent", "Nobody in front of the camera (leave the room / point lid away)"),
    ("present_away", "Sit normally, look away from the screen"),
    ("present_looking", "Sit normally, look straight at the camera"),
    ("close", "Face ~25 cm from the camera, looking at it"),
    ("backlit", "Bright lamp or window behind you, looking at camera"),
]
MAX_LIT = 150  # ~20 s at 7.5 lit fps


def run_once():
    r = m.msxu_reader("/dev/video2", warmup=0, max_saturation=1)
    n, means, t0 = 0, [], time.monotonic()
    while r.stats["restarts"] == 0 and n < MAX_LIT:
        img = r.read_gray()
        if img is None:
            break
        n += 1
        means.append(float(img.mean()))
    dt = time.monotonic() - t0
    r.release()
    lit = n - 1 if r.stats["restarts"] else n
    return lit, dt, sum(means[-5:]) / max(1, len(means[-5:])), r.stats["restarts"] == 0


def main():
    runs = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cutoff_results.csv")
    new = not os.path.exists(out)
    with open(out, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time", "scenario", "run", "lit_frames_before_cutoff", "seconds", "end_mean", "no_cutoff"])
        for key, desc in SCENARIOS:
            ans = input(f"\n[{key}] {desc}\nEnter to start, 's' to skip: ").strip().lower()
            if ans == "s":
                continue
            for i in range(runs):
                lit, dt, mean, capped = run_once()
                print(f"  run {i+1}: {lit} lit frames{' (no cut-off, capped)' if capped else ''} in {dt:.1f}s, mean {mean:.0f}")
                w.writerow([time.strftime("%F %T"), key, i + 1, lit, f"{dt:.2f}", f"{mean:.1f}", int(capped)])
                f.flush()
                time.sleep(1)
    print(f"\nresults in {out}")


if __name__ == "__main__":
    main()
