"""
Compare what the rig TRANSMITTED against what it was GIVEN.

    python correlate.py --soak soak_log.csv --sdi12 sdi12_served.csv
    python correlate.py --soak soak_log.csv --sdi12 sdi12_served.csv --river 4079

soak_log.csv     from protocol_soak.py - what went out over VHF
sdi12_served.csv from the gateway - what the ERT-A2 was handed on SDI-12,
                 produced by:  python3 /home/pi/sdi12_export.py > sdi12_served.csv

WHY THIS EXISTS
Reading a decode and deciding it "looks about right" is how this project lost
weeks. The only test that means anything is comparing a reading against an
independent record of what was actually sent. That technique - matching the
LoRa frame counter against what the sensor really transmitted - is what
eventually found the channel-plan fault after antenna, spreading-factor and
path-loss theories had all been chased and all been wrong.

The same idea applies here, in two forms:

  RIVER CHANNEL   we know the exact metres the ERT-A2 was served at each poll,
                  so for every transmitted reading we can look up the value in
                  force at that moment and derive the actual mapping. This is
                  the open question: we served 0.420 m and the SDR decoded 265,
                  which matches neither Level x1000 (420), nor Raw in mm
                  (1523), nor Raw-1000 (523). Two or more matched pairs solve
                  it outright.

  COUNTER CHANNEL a tipping-bucket relay on DI1 is a counter, so ground truth
                  needs nothing else: it must only ever increase, by small
                  steps, at roughly the pulse rate. Any decode that breaks
                  monotonicity is provably wrong, with no reference file at
                  all.

Everything printed is either measured or explicitly marked as an inference.
"""
import argparse
import csv
import statistics
from collections import defaultdict
from datetime import datetime

TS = "%Y-%m-%d %H:%M:%S"


def load(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def parse_ts(s):
    try:
        return datetime.strptime(s.strip(), TS)
    except Exception:
        return None


def as_num(s):
    try:
        return float(s)
    except Exception:
        return None


def section(title):
    print()
    print("=" * 74)
    print("  " + title)
    print("=" * 74)


def per_protocol_summary(rows):
    section("WHAT WAS HEARD, BY PROTOCOL AND ID")
    groups = defaultdict(list)
    for r in rows:
        groups[(r["protocol"], r.get("id_folded") or r["sensor_id"])].append(r)

    print(f"  {'protocol':17s} {'id':>6s} {'n':>5s} {'value range':>18s} "
          f"{'crc ok':>8s} {'bit12 set':>10s}")
    print("  " + "-" * 70)
    for (proto, sid), rs in sorted(groups.items()):
        vals = [as_num(r["raw_value"]) for r in rs]
        vals = [v for v in vals if v is not None]
        rng = f"{min(vals):g} .. {max(vals):g}" if vals else "-"
        crc = [r.get("crc_ok", "") for r in rs]
        ok = sum(1 for c in crc if str(c).lower() == "true")
        has = sum(1 for c in crc if str(c).lower() in ("true", "false"))
        crcs = f"{ok}/{has}" if has else "n/a"
        b12 = sum(1 for r in rs if str(r.get("id_bit12", "")) == "1")
        print(f"  {proto:17s} {str(sid):>6s} {len(rs):>5d} {rng:>18s} "
              f"{crcs:>8s} {b12:>10d}")

    # The same burst claimed by two protocols is the failure mode the apps
    # refuse to guess about. Surface it rather than quietly picking a winner.
    section("CROSS-TALK: one burst claimed by more than one protocol")
    by_time = defaultdict(set)
    for r in rows:
        t = parse_ts(r["timestamp"])
        if t:
            by_time[(t.replace(microsecond=0),
                     r.get("id_folded") or r["sensor_id"],
                     r["raw_value"])].add(r["protocol"])
    clashes = {k: v for k, v in by_time.items() if len(v) > 1}
    if not clashes:
        print("  none - every reading was claimed by exactly one protocol")
    else:
        print(f"  {len(clashes)} reading(s) claimed by two or more protocols:")
        for (t, sid, val), protos in sorted(clashes.items())[:20]:
            print(f"    {t:%H:%M:%S}  id={sid} value={val}  ->  "
                  f"{', '.join(sorted(protos))}")
        print("  These are not necessarily errors, but a value trusted from")
        print("  the wrong one of these invents a station. Check raw_hex.")


def river_mapping(rows, served, river_id):
    section(f"RIVER CHANNEL {river_id}: transmitted value vs what we served")
    if not served:
        print("  no SDI-12 ground truth supplied (--sdi12), skipping")
        return

    pts = []
    for r in rows:
        sid = r.get("id_folded") or r["sensor_id"]
        if str(sid) != str(river_id):
            continue
        t = parse_ts(r["timestamp"])
        raw = as_num(r["raw_value"])
        if t is None or raw is None:
            continue
        # The value in force is the last one served BEFORE this burst.
        prior = [s for s in served if s["_t"] and s["_t"] <= t]
        if not prior:
            continue
        s = prior[-1]
        if s["_v"] is None:
            continue                       # sentinel or unparsable
        age = (t - s["_t"]).total_seconds()
        pts.append((t, s["_v"], raw, age, r.get("raw_hex", ""),
                    r["protocol"], str(r.get("id_bit12", ""))))

    if not pts:
        print("  no transmitted readings could be matched to a served value")
        print()
        print("  If the soak logged rows for this ID, the two files are")
        print("  probably not in the same timezone: every transmission would")
        print("  then sit BEFORE the first poll and match nothing. The soak")
        print("  stamps naive local time; sdi12_export.py must be given the")
        print("  matching --tz (default Australia/Sydney), not the gateway's")
        print("  own UTC.")
        return

    # A poll runs every five minutes, so a transmission is normally matched to
    # a reading under 300 s old. A median age far past that does not mean slow
    # polling -- it means the two files are in different timezones, and the fit
    # below would be a tidy straight line through the wrong pairs. That failure
    # is invisible in the numbers, so it has to be caught here.
    ages = sorted(p[3] for p in pts)
    med_age = ages[len(ages) // 2]
    if med_age > 1800:
        print(f"  *** REFUSING TO SOLVE: median match age is {med_age:.0f} s "
              f"({med_age / 3600:.1f} h).")
        print("  Polls run every 5 minutes, so this is a clock/timezone")
        print("  mismatch between soak_log.csv and the SDI-12 export, not a")
        print("  data problem. Re-export with the timezone the soak logs in.")
        print("  Any mapping solved from these pairs would be confidently")
        print("  wrong, so none is offered.")
        return

    print(f"  {'time':>8s} {'served m':>9s} {'sent raw':>9s} {'age s':>6s} "
          f"{'bit12':>5s}  raw_hex")
    for t, sv, raw, age, hx, proto, b12 in pts[-25:]:
        print(f"  {t:%H:%M:%S} {sv:9.3f} {raw:9g} {age:6.0f} {b12:>5s}  {hx}")

    # Solve for a linear mapping. Two distinct pairs are enough; more is
    # better. If the residuals are large the mapping is not linear and the
    # numbers below are meaningless - which is itself the finding.
    live = [(sv, raw) for _t, sv, raw, _a, _h, _p, b in pts if b != "1"]
    uniq = sorted(set(live))
    print()
    if len(uniq) < 2:
        print(f"  only {len(uniq)} distinct live pair(s) - need 2+ to solve "
              f"the mapping. Let the level move, or use the counter channel.")
        return
    (x1, y1), (x2, y2) = uniq[0], uniq[-1]
    if x2 == x1:
        print("  served value never changed; cannot solve")
        return
    slope = (y2 - y1) / (x2 - x1)
    offset = y1 - slope * x1
    resid = [abs(raw - (slope * sv + offset)) for sv, raw in live]
    print(f"  best-fit through the extremes:  raw = {slope:.4f} * metres "
          f"+ {offset:.2f}")
    print(f"  residuals: max {max(resid):.2f}, mean "
          f"{statistics.fmean(resid):.2f}  (n={len(live)})")
    print()
    print(f"  for reference, the candidate mappings:")
    for name, f in (("Level x 1000 (mm)", lambda m: m * 1000),
                    ("Level x 100 (cm)", lambda m: m * 100),
                    ("Raw distance mm", lambda m: 1946.2 - m * 1000)):
        err = [abs(raw - f(sv)) for sv, raw in live]
        print(f"    {name:22s} mean error {statistics.fmean(err):9.1f}")
    if max(resid) > 2:
        print()
        print("  NOTE: residuals are large - the relationship is not a clean")
        print("  straight line. Do not read the slope above as the answer.")


def counter_channel(rows, counter_id):
    section(f"COUNTER CHANNEL {counter_id}: self-validating, no reference needed")
    seq = []
    for r in rows:
        sid = r.get("id_folded") or r["sensor_id"]
        if str(sid) != str(counter_id):
            continue
        t = parse_ts(r["timestamp"])
        v = as_num(r["raw_value"])
        if t and v is not None:
            seq.append((t, v, r["protocol"], r.get("raw_hex", "")))
    if len(seq) < 2:
        print(f"  {len(seq)} reading(s) - not enough to check")
        return

    seq.sort()
    steps, backwards = [], []
    for (t0, v0, _p0, _h0), (t1, v1, p1, h1) in zip(seq, seq[1:]):
        d = v1 - v0
        dt = (t1 - t0).total_seconds()
        if d < 0:
            backwards.append((t1, v0, v1, p1, h1))
        elif dt > 0:
            steps.append((d, dt))
    print(f"  readings      : {len(seq)}  from {seq[0][0]:%H:%M:%S} "
          f"to {seq[-1][0]:%H:%M:%S}")
    print(f"  value range   : {seq[0][1]:g} -> {seq[-1][1]:g}")
    if steps:
        incs = [d for d, _ in steps if d > 0]
        gaps = [dt for d, dt in steps if d > 0]
        if incs:
            print(f"  increments    : median {statistics.median(incs):g}, "
                  f"max {max(incs):g}")
            print(f"  interval      : median {statistics.median(gaps):.0f} s")
    if backwards:
        print(f"  *** {len(backwards)} DECREASE(S) - a counter must never go "
              f"backwards, so these decodes are provably wrong:")
        for t, v0, v1, p, h in backwards[:10]:
            print(f"      {t:%H:%M:%S}  {v0:g} -> {v1:g}  [{p}]  {h}")
    else:
        print("  monotonic     : yes - no decreasing readings")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--soak", required=True)
    ap.add_argument("--sdi12", help="ground truth for the river channel")
    ap.add_argument("--river", default="4079")
    ap.add_argument("--counter", default="4078",
                    help="the DI/relay channel ID")
    args = ap.parse_args()

    rows = load(args.soak)
    print(f"loaded {len(rows)} decoded readings from {args.soak}")

    served = []
    if args.sdi12:
        for s in load(args.sdi12):
            s["_t"] = parse_ts(s.get("timestamp", ""))
            v = as_num(s.get("value", ""))
            # -9.999 is the staleness sentinel, not a level.
            s["_v"] = None if (v is None or v < -9) else v
            served.append(s)
        served.sort(key=lambda s: s["_t"] or datetime.min)
        good = sum(1 for s in served if s["_v"] is not None)
        print(f"loaded {len(served)} SDI-12 polls from {args.sdi12} "
              f"({good} real, {len(served) - good} sentinel)")

    per_protocol_summary(rows)
    river_mapping(rows, served, args.river)
    counter_channel(rows, args.counter)
    print()


if __name__ == "__main__":
    main()
