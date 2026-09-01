#!/usr/bin/env bash
# Raw-IQ capture helper for ALERT/iFLOWS fine-tuning.
# Usage:  ./capture_iq.sh <label> [seconds] [gain] [ppm]
#   label   : describes the setup, e.g. nofilter_g40  or  filter_g40
#   seconds : capture length (default 300 = 5 min, to catch intermittent bursts)
#   gain    : tuner gain dB (default 40; try 30 / 40 / 49 to find best)
#   ppm     : freq correction (default 0)
#
# ALWAYS capture RAW IQ (rtl_sdr) - NOT SDR# audio. SDR# processing destroys
# the FSK bit timing and nothing decodes from it.
#
# A/B method: same antenna/gain, back-to-back:
#   ./capture_iq.sh nofilter_g40        <- baseline, filter NOT fitted
#   (fit the SBP-150+ filter)
#   ./capture_iq.sh filter_g40          <- with filter, same gain
# then compare decode counts.

set -u
LABEL="${1:?need a label, e.g. nofilter_g40}"
SECS="${2:-300}"
GAIN="${3:-40}"
PPM="${4:-0}"
FREQ=151500000
RATE=1024000
OUTDIR="C:/SDR ALERT Decoder/captures"
mkdir -p "$OUTDIR"
STAMP=$(date +%Y%m%d_%H%M%S)
OUT="$OUTDIR/iq_${LABEL}_g${GAIN}_${STAMP}.bin"

# free the device
taskkill //F //IM "SDR ALERT Decoder.exe" 2>/dev/null
taskkill //F //IM "rtl_sdr.exe" 2>/dev/null
taskkill //F //IM "rtl_fm.exe" 2>/dev/null
sleep 1

echo "Capturing ${SECS}s raw IQ @ ${FREQ} Hz, ${RATE} S/s, gain ${GAIN}, ppm ${PPM}"
echo "  -> $OUT"
echo "  (keep signals active; Ctrl-C to stop early)"
timeout "$SECS" rtl_sdr -f "$FREQ" -s "$RATE" -g "$GAIN" -p "$PPM" "$OUT"
SZ=$(stat -c%s "$OUT" 2>/dev/null || echo 0)
echo "Done: $(python -c "print(f'{$SZ/2/$RATE:.0f}s, {$SZ/1e6:.0f}MB')" 2>/dev/null)"
echo "Decode it with the app's 'Decode IQ capture (.bin)' button, or in src:"
echo "  python -c \"import numpy as np;from iq_decoder import IQAlertDecoder;..."
