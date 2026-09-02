"""Stage the payload and build the Windows installer.

    python installer/build_installer.py
    python installer/build_installer.py --dist "C:/SDR ALERT Decoder/src/dist/SDR ALERT Decoder"

The second form packages a build made outside this clone - on maddog the app
is built in the working copy at C:\SDR ALERT Decoder, not in the repo.

Requires Inno Setup 6 (ISCC.exe) and a current PyInstaller one-dir build in
src/dist. Refuses to build a stale or incomplete payload rather than shipping
one - a stale build has silently shipped a decoder with no demodulator in it
before, and an installer makes that mistake much harder to notice.

Deliberately does NOT include Sensors.xlsx: site registers are agency data and
not ours to redistribute.
"""
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "src")
DIST = os.path.join(SRC, "dist", "SDR ALERT Decoder")

if "--dist" in sys.argv:
    DIST = os.path.abspath(sys.argv[sys.argv.index("--dist") + 1])
    # <src>/dist/<app>  ->  <src>, so the staleness check reads the sources
    # that build actually came from rather than this clone's.
    SRC = os.path.dirname(os.path.dirname(DIST))
PAYLOAD = os.path.join(HERE, "payload")
EXE_NAME = "SDR ALERT Decoder.exe"

# Same watch list deploy.py uses: the build must be newer than all of them.
WATCHED = ["fsk_pll_decode.py", "iq_decoder.py", "sensor_database.py",
           "field_application.py", "iq_sdr_interface.py",
           "alert2.py", "alert2_app.py", "alert2_decoder.py"]
REQUIRED_MODULES = [b"fsk_pll_decode", b"iq_decoder", b"sensor_database",
                    b"field_application", b"alert2", b"alert2_decoder"]

# rtl_sdr.exe and rtl_test.exe are what the app actually invokes; the DLLs are
# what they need to start. rtl_fm/rtl_tcp are included because they are the
# tools an operator reaches for when diagnosing a dead dongle.
RTL_SRC_CANDIDATES = [r"C:\rtl-sdr\x64", r"C:\rtl-sdr"]
RTL_FILES = ["rtl_sdr.exe", "rtl_test.exe", "rtl_fm.exe", "rtl_tcp.exe",
             "rtlsdr.dll", "pthreadVC2.dll", "msvcr100.dll"]
RTL_SOURCE_TREE = r"C:\rtl-sdr\rtl-sdr-blog-1.3.6"     # for COPYING


def fail(msg):
    print(f"[FAIL] {msg}")
    sys.exit(1)


def find_iscc():
    local = os.environ.get("LOCALAPPDATA", "")
    for p in (r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
              r"C:\Program Files\Inno Setup 6\ISCC.exe",
              # Inno Setup 6 installed per-user (no admin rights needed)
              os.path.join(local, "Programs", "Inno Setup 6", "ISCC.exe")):
        if os.path.exists(p):
            return p
    found = shutil.which("ISCC")
    if found:
        return found
    fail("Inno Setup 6 not found. Install it (jrsoftware.org/isdl.php) or "
         "put ISCC.exe on PATH.")


def stage():
    exe_src = os.path.join(DIST, EXE_NAME)
    if not os.path.exists(exe_src):
        fail(f"no PyInstaller build at {exe_src} - build it first")

    built = os.path.getmtime(exe_src)
    stale = [f for f in WATCHED
             if os.path.exists(os.path.join(SRC, f))
             and os.path.getmtime(os.path.join(SRC, f)) > built]
    if stale:
        fail("the build is STALE, rebuild before packaging: " + ", ".join(stale))

    data = open(exe_src, "rb").read()
    missing = [m.decode() for m in REQUIRED_MODULES if m not in data]
    if missing:
        fail("build is missing modules: " + ", ".join(missing))
    print(f"build verified ({(time.time()-built)/60:.1f} min old, "
          f"{len(data)/1e6:.0f} MB)")

    if os.path.isdir(PAYLOAD):
        shutil.rmtree(PAYLOAD)
    os.makedirs(PAYLOAD)
    shutil.copy2(exe_src, os.path.join(PAYLOAD, EXE_NAME))
    shutil.copytree(os.path.join(DIST, "_internal"),
                    os.path.join(PAYLOAD, "_internal"))

    rtl_dir = next((d for d in RTL_SRC_CANDIDATES
                    if os.path.exists(os.path.join(d, "rtl_sdr.exe"))), None)
    if not rtl_dir:
        fail("rtl-sdr tools not found in " + " or ".join(RTL_SRC_CANDIDATES))
    out_rtl = os.path.join(PAYLOAD, "rtl-sdr")
    os.makedirs(out_rtl)
    for name in RTL_FILES:
        p = os.path.join(rtl_dir, name)
        if not os.path.exists(p):
            fail(f"rtl-sdr tool missing: {p}")
        shutil.copy2(p, os.path.join(out_rtl, name))
    # GPL: the binaries above ship with their licence text alongside.
    copying = os.path.join(RTL_SOURCE_TREE, "COPYING")
    if os.path.exists(copying):
        shutil.copy2(copying, os.path.join(out_rtl, "COPYING"))
    else:
        fail("rtl-sdr COPYING not found - the GPL tools cannot ship without "
             "their licence text")
    print(f"staged {len(RTL_FILES)} rtl-sdr files from {rtl_dir}")

    overrides = os.path.join(SRC, "sensor_overrides.json")
    if os.path.exists(overrides):
        shutil.copy2(overrides, os.path.join(PAYLOAD, "sensor_overrides.json"))

    # Site registers are agency data. Never package one.
    for bad in ("Sensors.xlsx", "sensors.xlsx", "site_metadata.csv"):
        p = os.path.join(PAYLOAD, bad)
        if os.path.exists(p):
            os.remove(p)
            print(f"[WARN] removed {bad} from the payload - agency data")


def main():
    iscc = find_iscc()
    stage()
    script = os.path.join(HERE, "SDR-ALERT-Decoder.iss")
    r = subprocess.run([iscc, script], capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stdout[-3000:])
        print(r.stderr[-2000:])
        fail(f"ISCC failed with exit code {r.returncode}")

    outdir = os.path.join(ROOT, "dist")
    setups = [f for f in os.listdir(outdir) if f.lower().endswith(".exe")] \
        if os.path.isdir(outdir) else []
    if not setups:
        fail("ISCC reported success but produced no installer")
    for f in sorted(setups):
        p = os.path.join(outdir, f)
        print(f"built {f}  ({os.path.getsize(p)/1e6:.0f} MB)")
        print(f"  {p}")


if __name__ == "__main__":
    main()
