"""
Phase 11 smoke — Identity, recognition & access control.

Verifies everything checkable without a live mic/camera: encrypted-at-rest storage, the
voiceprint engine (real same/different-speaker test via Edge-TTS when online), the trust
resolver (open mode, tiers, unknown-voice, cache re-verification, face second-factor), tool
gating by tier (owner/trusted/guest/stranger), the passphrase gate, the agent's schema filter
+ execution backstop, the management tools, the router (auth + status), and the persona block.

Run:  python scripts/identity_smoke.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    PASS += 1 if cond else 0
    FAIL += 0 if cond else 1
    print(f"  {'OK ' if cond else 'XX '}{name}" + (f"  — {detail}" if detail and not cond else ""))


def unit(seed: int, d: int = 256) -> np.ndarray:
    r = np.random.RandomState(seed); v = r.randn(d).astype("float32"); return v / np.linalg.norm(v)


def main() -> None:
    # isolate the encrypted roster in a temp dir
    import app.services.identity.store as sm
    sm._store = sm.IdentityStore(Path(tempfile.mkdtemp()) / "id.db")

    from app.services import identity as ident
    from app.services.identity import crypto, trust as T, voiceprint as vp, face as fz
    from app.services.identity.store import get_store
    from config import IDENTITY_VOICE_THRESHOLD, IDENTITY_DIR

    print("\n[1] crypto: encrypt-at-rest roundtrip")
    fk = IDENTITY_DIR / ".fmk"
    blob = crypto.protect(b"a-secret-voiceprint", fk)
    check("protect tags + grows the blob", blob[:1] in (b"D", b"F") and len(blob) > 19)
    check("unprotect recovers the plaintext", crypto.unprotect(blob, fk) == b"a-secret-voiceprint")
    try:
        crypto.unprotect(b"D\x00\x01\x02bad", fk); tampered = False
    except Exception:
        tampered = True
    check("tampered ciphertext is rejected", tampered)

    print("\n[2] store: encrypted vectors + CRUD + passphrase")
    st = get_store()
    OWN, VIK, GUE = unit(1), unit(2), unit(3)
    st.add("aditya", "owner", OWN, display="Aditya")
    st.add("vikram", "trusted", VIK, display="Vikram")
    st.add("sam", "guest", GUE, display="Sam")
    check("count == 3", st.count() == 3)
    check("encrypted voiceprint roundtrips exactly", np.allclose(st.get("aditya").voice, OWN))
    check("owner() finds the owner", st.owner() and st.owner().name == "aditya")
    check("remove() works", st.remove("sam") and st.count() == 2)
    st.set_passphrase("authorize delta seven")
    check("has_passphrase after set", st.has_passphrase())
    check("passphrase matches inside a sentence", st.check_passphrase("ok, authorize delta seven now"))
    check("wrong passphrase rejected", not st.check_passphrase("open the door"))

    print("\n[3] voiceprint engine (real speech via Edge-TTS — skipped offline)")
    try:
        import asyncio, io, edge_tts, librosa

        async def synth(text, voice):
            buf = io.BytesIO()
            async for ch in edge_tts.Communicate(text, voice).stream():
                if ch["type"] == "audio":
                    buf.write(ch["data"])
            buf.seek(0); y, sr = librosa.load(buf, sr=16000, mono=True); return y

        async def go():
            a = await synth("Good evening sir, all systems online and standing by.", "en-GB-RyanNeural")
            b = await synth("The markets are mixed and bitcoin is up three percent.", "en-GB-RyanNeural")
            c = await synth("Hello there, this is a totally different person speaking.", "en-US-AriaNeural")
            return vp.embed(a), vp.embed(b), vp.embed(c)
        ea, eb, ec = asyncio.run(go())
        same, diff = vp.similarity(ea, eb), vp.similarity(ea, ec)
        check("256-d voiceprints produced", all(e is not None and e.shape == (256,) for e in (ea, eb, ec)))
        check(f"same speaker matches (sim={same:.2f} vs thr {IDENTITY_VOICE_THRESHOLD})", same >= IDENTITY_VOICE_THRESHOLD)
        check(f"different speaker rejected (sim={diff:.2f} vs thr {IDENTITY_VOICE_THRESHOLD})", diff < IDENTITY_VOICE_THRESHOLD)
    except Exception as e:  # noqa: BLE001
        print(f"   (skipped - {type(e).__name__}: offline or TTS unavailable)")

    print("\n[4] trust: classify, tiers, cache, face second-factor")
    T.reset()
    orig = vp.embed
    for label, vec, exp in [("owner", OWN, "owner"), ("trusted", VIK, "trusted"), ("unknown", unit(9), "stranger")]:
        vp.embed = lambda *a, **k: vec
        t = T.identify_voice(np.zeros(16000, "int16"))
        check(f"voice '{label}' -> {exp}", t.tier == exp, f"got {t.tier}")
    # REGRESSION: a believable-but-imperfect clip (in the floor band, ~0.65) is UNSURE not stranger,
    # so the Owner is never deflected on a noisy/casual turn — it reuses his established trust.
    band = 0.65 * OWN + 0.76 * unit(9)
    band = band / np.linalg.norm(band)
    vp.embed = lambda *a, **k: band
    tb = T.identify_voice(np.zeros(16000, "int16"))
    check(f"borderline voice -> 'unsure' not 'stranger' (sim={vp.similarity(band, OWN):.2f})",
          tb.tier == "unsure", f"got {tb.tier}")
    vp.embed = orig
    check("non-voice surface defaults to owner (device trust)",
          T.resolve("pwa_chat").tier == "owner")
    # cache: a too-short clip reuses the established trust
    T.reset()
    vp.embed = lambda *a, **k: VIK
    T.resolve("pc_voice", voice=np.zeros(16000, "int16"))           # establishes 'trusted'
    vp.embed = lambda *a, **k: None                                  # next clip too short -> unsure
    check("unsure clip reuses cached trust", T.resolve("pc_voice", voice=np.zeros(16000, "int16")).tier == "trusted")
    vp.embed = orig
    # face second factor: matching face keeps owner; a different face demotes to stranger
    st.add("aditya", "owner", OWN, face=unit(11, 128), display="Aditya")
    of = fz.embed_largest
    fz.embed_largest = lambda img: unit(11, 128)
    check("matching face confirms identity",
          T.confirm_face(T.Trust(tier="owner", name="aditya"), np.zeros((8, 8, 3), "uint8")).tier == "owner")
    fz.embed_largest = lambda img: unit(99, 128)
    check("mismatched face demotes to stranger",
          T.confirm_face(T.Trust(tier="owner", name="aditya"), np.zeros((8, 8, 3), "uint8")).tier == "stranger")
    fz.embed_largest = of

    print("\n[5] tool gating matrix")
    own, tru, gue, str = (T.Trust(tier="owner"), T.Trust(tier="trusted"),
                          T.Trust(tier="guest"), T.Trust(tier="stranger"))
    check("owner may send_whatsapp", T.tool_allowed("owner", own))
    check("trusted may NOT send_whatsapp", not T.tool_allowed("owner", tru))
    check("trusted may set_timer", T.tool_allowed("trusted", tru))
    check("guest may NOT set_timer", not T.tool_allowed("trusted", gue))
    check("guest may web_search", T.tool_allowed("guest", gue))
    check("stranger may do NOTHING", not T.tool_allowed("guest", str))
    own_phrase = T.Trust(tier="owner", passphrase_ok=True)
    check("owner+passphrase needs the phrase (denied without)", not T.tool_allowed("owner+passphrase", own))
    check("owner+passphrase allowed WITH the phrase", T.tool_allowed("owner+passphrase", own_phrase))
    check("trusted blocked from owner+passphrase", not T.tool_allowed("owner+passphrase", tru))
    # hardened: with NO passphrase configured the gate stays CLOSED (no silent degrade to owner)
    get_store().set_passphrase("")
    check("owner+passphrase blocked when none is configured",
          not T.tool_allowed("owner+passphrase", T.Trust(tier="owner", passphrase_ok=get_store().check_passphrase("x"))))
    get_store().set_passphrase("authorize delta seven")
    check("prompt_line names the speaker tier", "TRUSTED" in T.prompt_line(tru) and "UNKNOWN" in T.prompt_line(str))

    print("\n[6] tools: registration, tiers, behaviour")
    import app.tools as tools
    tools.discover()
    check("tier policy applied (web_search=guest)", tools.get("web_search").tier == "guest")
    check("tier policy applied (set_timer=trusted)", tools.get("set_timer").tier == "trusted")
    check("send_whatsapp stays owner", tools.get("send_whatsapp").tier == "owner")
    check("remove_access is owner+passphrase + terminal",
          tools.get("remove_access").tier == "owner+passphrase" and tools.get("remove_access").terminal)
    check("list_access shows the roster", "Aditya" in tools.get("list_access").run({}))
    check("set_security_passphrase confirms", "passphrase" in tools.get("set_security_passphrase").run({"passphrase": "blue forty two"}).lower())
    check("enroll_person opens a guided face+voice capture",
          "camera" in tools.get("enroll_person").run({"name": "Riya", "tier": "trusted"}).lower())
    ident.cancel_enrollment()   # don't leave a pending session for later sections

    print("\n[7] agent: schema filter (visibility) + execution backstop")
    vis_owner = lambda t: ident.tool_visible(t.tier, own)
    vis_guest = lambda t: ident.tool_visible(t.tier, gue)
    vis_stranger = lambda t: ident.tool_visible(t.tier, str)
    allow_owner = lambda t: ident.tool_allowed(t.tier, own)
    allow_guest = lambda t: ident.tool_allowed(t.tier, gue)
    total = len(tools.for_openai())
    check("owner SEES every tool (incl. passphrase-gated)", len(tools.for_openai(vis_owner)) == total)
    check("guest sees only a couple", 0 < len(tools.for_openai(vis_guest)) < total)
    check("stranger sees no tools", len(tools.for_openai(vis_stranger)) == 0)
    from app.services.agent.runner import AgentRunner, ToolTrace
    runner = AgentRunner()
    # a guest's forbidden owner-tool call is refused at execution (never actually runs)
    trace: list = []
    out = runner._exec("send_whatsapp", {"contact": "x", "text": "y"}, trace, allow_guest)
    check("backstop blocks a forbidden call (not executed)",
          "denied" in out.lower() and trace and trace[-1].error == "forbidden")
    # owner sees remove_access, but execution without the passphrase asks for it (doesn't run)
    trace2: list = []
    out2 = runner._exec("remove_access", {"name": "nobody"}, trace2, allow_owner)
    check("owner+passphrase tool asks for the phrase when unspoken",
          "passphrase" in out2.lower() and trace2 and trace2[-1].error == "forbidden")

    print("\n[8] router: mount + auth")
    from fastapi.testclient import TestClient
    from app.main import app
    paths = {r.path for r in app.routes}
    for p in ("/identity/status", "/identity/verify", "/identity/enroll", "/identity/remove",
              "/identity/passphrase", "/identity/roster"):
        check(f"route {p} mounted", p in paths)
    client = TestClient(app)
    check("/identity/status is open", client.get("/identity/status").status_code == 200)
    from config import IDENTITY_TOKEN
    check("enroll rejects a missing token (401)",
          client.post("/identity/enroll", data={"name": "x", "tier": "guest"}).status_code == 401)
    check("remove accepts the right token",
          client.post("/identity/remove", json={"name": "nobody"},
                      headers={"x-jarvis-token": IDENTITY_TOKEN}).status_code == 200)

    print("\n[9] persona + chat trust plumbing")
    from config import build_system_prompt
    sp = build_system_prompt()
    check("ACCESS CONTROL persona block present", "ACCESS CONTROL" in sp)
    check("persona forbids leaking the mechanism", "machinery" in sp.lower() or "in character" in sp.lower())
    get_store().set_passphrase("authorize delta seven")   # set a known phrase for this check
    from app.routers.web import _trust_for, ChatReq
    tr = _trust_for(ChatReq(text="please authorize delta seven", speaker_tier="owner", speaker_name="Aditya"))
    check("chat builds owner trust + checks passphrase from text", tr.is_owner and tr.passphrase_ok)
    tr2 = _trust_for(ChatReq(text="hi", speaker_tier="trusted", speaker_name="Vikram"))
    check("chat builds trusted trust (no passphrase)", tr2.tier == "trusted" and not tr2.passphrase_ok)
    check("no speaker fields -> owner default (None trust)", _trust_for(ChatReq(text="hi")) is None)

    print("\n[10] active identity + face ID + guided enrolment + new endpoints")
    import app.services.identity.face as fz2
    import app.services.identity.voiceprint as vp2
    T.set_active(T.Trust(tier="trusted", name="vikram", display="Vikram", confidence=0.82, source="voice"))
    check("active_view reflects who's using JARVIS",
          ident.active_view()["line"].startswith("VIKRAM") and ident.active_view()["tier"] == "trusted")
    T.set_active(T.Trust(tier="stranger"))
    check("a stranger never becomes the active user", ident.active_view()["tier"] == "trusted")
    # face identification (inject the SFace embedder)
    st.add("aditya", "owner", OWN, face=unit(11, 128), display="Aditya")
    ofc = fz2.embed_largest
    fz2.embed_largest = lambda img: unit(11, 128)
    check("identify_face matches the enrolled owner face", T.identify_face(np.zeros((8, 8, 3), "uint8")).tier == "owner")
    fz2.embed_largest = lambda img: unit(77, 128)
    check("identify_face rejects an unknown face", T.identify_face(np.zeros((8, 8, 3), "uint8")).tier == "stranger")
    # guided enrolment session (face + voice over a few turns)
    ov = vp2.embed
    vp2.embed = lambda *a, **k: unit(55)
    fz2.embed_largest = lambda img: unit(56, 128)
    check("request_enrollment opens a session",
          ident.request_enrollment("Riya", "trusted")["ok"] and ident.pending_enrollment()["name"] == "Riya")
    for _ in range(3):
        ident.add_pending_voice(np.zeros(16000, "int16"))
    ident.add_pending_face(np.zeros((8, 8, 3), "uint8"))
    fin = ident.finalize_enrollment()
    check("finalize enrols the new person with face + voice",
          fin["ok"] and get_store().get("riya") is not None and fin["face"])
    check("pending session cleared after finalize", ident.pending_enrollment() is None)
    vp2.embed, fz2.embed_largest = ov, ofc
    check("reverify_user tool registered at guest tier",
          tools.get("reverify_user") is not None and tools.get("reverify_user").tier == "guest")
    for p in ("/identity/active", "/identity/scan", "/identity/enroll/pending",
              "/identity/enroll/voice", "/identity/enroll/finalize", "/identity/enroll/cancel"):
        check(f"route {p} mounted", p in paths)
    ra = client.get("/identity/active")
    check("/identity/active open + returns a tier", ra.status_code == 200 and "tier" in ra.json())
    check("/identity/scan needs the token", client.post("/identity/scan").status_code == 401)

    print("\n[11] mobile recognition: /whoami -> server-verified session tier -> /chat gating")
    import io, soundfile as sf
    import app.services.identity.voiceprint as vp3
    bio = io.BytesIO(); sf.write(bio, np.zeros(16000, dtype="float32"), 16000, format="WAV")
    wavb = bio.getvalue()
    st.add("aditya", "owner", OWN, display="Aditya")
    st.add("vikram", "trusted", VIK, display="Vikram")
    ov3 = vp3.embed
    vp3.embed = lambda *a, **k: OWN                  # owner's voice
    r = client.post("/identity/whoami", data={"session": "phoneA"}, files={"file": ("u.wav", wavb, "audio/wav")})
    check("/whoami recognises the owner", r.status_code == 200 and r.json()["tier"] == "owner")
    check("phone owner -> owner (never locked out)", _trust_for(ChatReq(text="send a whatsapp", session_id="phoneA")) is None)
    vp3.embed = lambda *a, **k: unit(123)            # an unknown voice
    r2 = client.post("/identity/whoami", data={"session": "phoneB"}, files={"file": ("u.wav", wavb, "audio/wav")})
    check("/whoami flags a stranger", r2.status_code == 200 and r2.json()["tier"] == "stranger")
    trs = _trust_for(ChatReq(text="send a whatsapp", session_id="phoneB"))
    check("phone stranger -> stranger trust (gated)", trs is not None and trs.is_stranger)
    vp3.embed = lambda *a, **k: VIK                  # a trusted friend
    client.post("/identity/whoami", data={"session": "phoneC"}, files={"file": ("u.wav", wavb, "audio/wav")})
    trt = _trust_for(ChatReq(text="what's the weather", session_id="phoneC"))
    check("phone trusted -> trusted trust (downgrade)", trt is not None and trt.tier == "trusted")
    vp3.embed = ov3
    check("/identity/whoami is OPEN (no token)",
          client.post("/identity/whoami", files={"file": ("u.wav", wavb, "audio/wav")}).status_code == 200)

    print("\n[12] mobile enrolment: owner-session authorises the enrol endpoints (no token shipped)")
    ident.set_session_trust("ownSess", T.Trust(tier="owner", name="aditya", display="Aditya"))
    ident.set_session_trust("guestSess", T.Trust(tier="guest", name="sam"))
    check("enrol allowed from a verified-OWNER session",
          client.get("/identity/enroll/pending?session=ownSess").status_code == 200)
    check("enrol REFUSED from a guest session (401)",
          client.get("/identity/enroll/pending?session=guestSess").status_code == 401)
    check("enrol REFUSED with no auth at all (401)",
          client.get("/identity/enroll/pending").status_code == 401)
    from config import IDENTITY_TOKEN as ITOK
    check("enrol still allowed with the master token",
          client.get("/identity/enroll/pending", headers={"x-jarvis-token": ITOK}).status_code == 200)
    # full owner-session-driven enrolment of a new person
    ident.cancel_enrollment()
    ident.request_enrollment("Maya", "trusted")
    vp3.embed = lambda *a, **k: unit(202)
    for _ in range(3):
        client.post("/identity/enroll/voice?session=ownSess", files={"file": ("v.wav", wavb, "audio/wav")})
    fin = client.post("/identity/enroll/finalize?session=ownSess")
    vp3.embed = ov3
    check("owner-session enrolment finalises + stores the person",
          fin.status_code == 200 and fin.json().get("ok") and get_store().get("maya") is not None)

    print(f"\n==== identity smoke: {PASS} passed, {FAIL} failed ====")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
