"""
Enrol a voice (and optional face) with JARVIS — Phase 11.

This is the one-time capture step JARVIS can't do from a single voice command. It records a few
spoken sentences from the mic, (optionally) a few webcam frames, and files the person under a
trust tier. Everything is stored encrypted (DPAPI) by the identity service — run locally, no
network, nothing leaves the machine.

  python scripts/enroll_identity.py owner                 # enrol yourself (full access)
  python scripts/enroll_identity.py trusted Vikram        # a family member / close friend
  python scripts/enroll_identity.py guest Sam             # a one-off guest (Q&A only)
  python scripts/enroll_identity.py face                  # add/refresh just YOUR face (keeps voice)
  python scripts/enroll_identity.py face Vikram           # add/refresh someone's face
  python scripts/enroll_identity.py list                  # show who's enrolled
  python scripts/enroll_identity.py remove Vikram         # revoke someone
  python scripts/enroll_identity.py passphrase "authorize delta seven"   # set the security phrase
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import SAMPLE_RATE, JARVIS_USER_NAME  # noqa: E402
from app.services import identity  # noqa: E402

SENTENCES = [
    "The arc reactor is stable and running at full output.",
    "Run a complete diagnostic on the main systems, please.",
    "Good evening — all protocols are online and standing by.",
    "Remind me about the meeting at nine tomorrow morning.",
    "Pull up the weather and today's headlines for me.",
]
REC_SECONDS = 4.0


def _record(seconds: float) -> np.ndarray:
    import sounddevice as sd
    for n in (3, 2, 1):
        print(f"   recording in {n}…", end="\r", flush=True); time.sleep(0.7)
    print("   ● speak now…            ", end="\r", flush=True)
    audio = sd.rec(int(seconds * SAMPLE_RATE), samplerate=SAMPLE_RATE, channels=1, dtype="int16")
    sd.wait()
    print("   ✓ got it.                ")
    return audio.reshape(-1)


def _capture_faces(n: int = 3) -> list:
    """Grab a few webcam frames (BGR) using the SAME robust capture the vision service uses —
    it probes DSHOW -> MSMF -> default backends across indices, warms up, and picks the sharpest
    frame. A plain cv2.VideoCapture(0) fails on many Windows webcams (DSHOW exception)."""
    try:
        from app.services.vision.camera import capture_webcam   # robust multi-backend capture
        import numpy as np
    except Exception as e:  # noqa: BLE001
        print(f"   (camera unavailable — skipping face: {e})"); return []
    print("   Look at the camera…")
    frames = []
    for i in range(n):
        try:
            pil = capture_webcam()                               # PIL RGB
            frames.append(np.array(pil)[:, :, ::-1].copy())      # -> OpenCV BGR
        except Exception as e:  # noqa: BLE001
            if i == 0:
                print(f"   (no camera — skipping face: {e})")
                return []
            break                                                # got at least one; good enough
    return frames


def enroll(tier: str, name: str) -> None:
    count = 5 if tier == "owner" else 3
    print(f"\nEnrolling '{name}' as {tier.upper()}. Read each line aloud, naturally.\n")
    clips = []
    for i in range(count):
        print(f'[{i+1}/{count}]  "{SENTENCES[i % len(SENTENCES)]}"')
        input("        press Enter, then read it…")
        clips.append(_record(REC_SECONDS))

    faces = []
    if input("\nAdd a face for the camera second-factor? [y/N] ").strip().lower().startswith("y"):
        faces = _capture_faces(3)

    print("\nProcessing voiceprint…")
    if tier == "owner":
        res = identity.enroll_owner(clips, sr=SAMPLE_RATE, face_imgs=faces or None, display=name)
    else:
        res = identity.enroll(name=name, tier=tier, clips=clips, sr=SAMPLE_RATE,
                              face_imgs=faces or None, display=name)
    print(("✅ " if res.get("ok") else "❌ ") + res.get("message", ""))
    if res.get("ok") and tier == "owner" and not identity.get_store().has_passphrase():
        print("\nTip: set a security passphrase for the most sensitive actions:")
        print('   python scripts/enroll_identity.py passphrase "authorize delta seven"')


def add_face(name: str) -> None:
    """Add (or refresh) just the FACE for someone already enrolled — keeps their voiceprint."""
    from app.services.identity.store import get_store
    from app.services.identity import face as facemod
    st = get_store()
    idn = st.get(name)
    if idn is None or idn.voice is None:
        print(f"'{name}' isn't fully enrolled yet — run the voice enrolment first "
              f"(e.g. 'owner' or 'trusted {name}')."); return
    imgs = _capture_faces(3)
    if not imgs:
        print("Couldn't reach the webcam, sir — make sure it's enabled and not in use by another app.")
        return
    feats = [f for f in (facemod.embed_largest(i) for i in imgs) if f is not None]
    if not feats:
        print("The camera worked but I couldn't find a clear face — look straight at it, good light, and retry.")
        return
    st.add(name=idn.name, tier=idn.tier, voice=idn.voice, face=facemod.average(feats),
           display=idn.display, samples=idn.samples)
    print(f"✅ Added {idn.display}'s face — face + voice both enrolled now.")


def show_list() -> None:
    roster = identity.roster()
    if not roster:
        print("No one is enrolled yet — JARVIS is in OPEN mode (answers to anyone).")
        print(f"Lock him to you:  python scripts/enroll_identity.py owner")
        return
    print("Enrolled:")
    for r in roster:
        print(f"  - {r['display']:<16} {r['tier']:<8}{' (+face)' if r['face'] else ''}")


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(__doc__); return
    cmd = args[0].lower()
    if cmd == "list":
        show_list()
    elif cmd == "owner":
        enroll("owner", args[1] if len(args) > 1 else JARVIS_USER_NAME)
    elif cmd in ("trusted", "guest"):
        if len(args) < 2:
            print(f"Usage: python scripts/enroll_identity.py {cmd} <name>"); return
        enroll(cmd, " ".join(args[1:]))
    elif cmd == "face":
        add_face(args[1] if len(args) > 1 else JARVIS_USER_NAME)
    elif cmd == "remove":
        if len(args) < 2:
            print("Usage: python scripts/enroll_identity.py remove <name>"); return
        print(identity.remove(" ".join(args[1:]))["message"])
    elif cmd == "passphrase":
        print(identity.set_passphrase(" ".join(args[1:]))["message"])
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
