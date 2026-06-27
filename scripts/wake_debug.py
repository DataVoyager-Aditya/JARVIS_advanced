"""
Wake-word DEBUG / calibration tool.

Shows, live:
  - your input devices (and which one is the default),
  - the mic level (RMS bar) so you can confirm sound is actually coming in,
  - what Vosk is transcribing in real time (partial -> final),
  - a big ">>> WAKE MATCHED" line whenever the wake phrase is detected.

Run:
    python scripts/wake_debug.py
Then say "wake up jarvis" a few times. Read the transcript it prints — if it shows
something like "wake up service" we widen the matcher; if it shows nothing/garbage,
it's a mic/level/device problem and we pick a device below.

Pick a specific mic:  python scripts/wake_debug.py --device 2
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import sounddevice as sd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import SAMPLE_RATE, VOSK_MODEL_PATH, WAKE_PHRASE  # noqa: E402
from app.services.voice.wake_vosk import VoskWake  # noqa: E402

BLOCK = 512


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", type=int, default=None, help="input device index (see list)")
    args = ap.parse_args()

    print("\n=== INPUT DEVICES ===")
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            default = "  <- DEFAULT" if i == sd.default.device[0] else ""
            print(f"  [{i}] {d['name']}  ({int(d['default_samplerate'])} Hz){default}")
    print(f"\nWake phrase: '{WAKE_PHRASE}'  |  model: {VOSK_MODEL_PATH}")
    print("Speak now. Say 'wake up jarvis'. Ctrl+C to stop.\n")

    wake = VoskWake()
    last_partial = ""
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                        blocksize=BLOCK, device=args.device) as stream:
        try:
            while True:
                block, _ = stream.read(BLOCK)
                b = block.reshape(-1).astype(np.int16)
                rms = float(np.sqrt(np.mean((b.astype(np.float32) / 32768.0) ** 2)))
                bar = "#" * min(40, int(rms * 200))

                ev = wake.process(b)
                partial = json.loads(wake.rec.PartialResult()).get("partial", "")
                if partial and partial != last_partial:
                    print(f"  heard: {partial!r}")
                    last_partial = partial
                if ev is not None:
                    print(f"\n>>> WAKE MATCHED  command={ev.command!r}\n")
                    last_partial = ""
                # level meter (overwrites same line)
                print(f"\rmic |{bar:<40}| rms={rms:.3f}", end="", flush=True)
        except KeyboardInterrupt:
            print("\nstopped.")


if __name__ == "__main__":
    main()
