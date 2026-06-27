# JARVIS_advanced

A free, voice-driven JARVIS — refined British male voice, wakes on **"Wake up JARVIS"**,
runs entirely on free-tier APIs and offline models. Built phase by phase, each feature final
on first build.

> Rules: [.claude/RULES.md](.claude/RULES.md) · Progress: [STATUS.md](STATUS.md) · Roadmap: [PLANNER.md](PLANNER.md)

---

## Setup (start here)

You need **Python 3.10+**, **git**, and **Windows** (JARVIS uses Windows-native integrations).
A working microphone + **headphones** (so he doesn't hear his own voice) are needed for the
voice loop.

### 1. Clone

```powershell
git clone <repo-url> JARVIS_advanced
cd JARVIS_advanced
```

### 2. Create a virtual environment & install dependencies

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

> This pulls in `torch`, `transformers`, `opencv`, etc. — it's a large install (a few GB)
> and can take a while the first time.

### 3. Create your `.env`

The `.env` file holds secrets and is **not** committed to git — each person creates their own
in the project root. Minimum to get talking is a single **free** Groq key:

```ini
# --- Required: free Groq key (chat + Whisper STT fallback + vision) ---
# Get one free at https://console.groq.com/keys
GROQ_API_KEY=gsk_your_key_here

# --- Voice (free, no key needed) ---
# Edge-TTS is the default — Microsoft, unlimited, no account. Leave as-is.
EDGE_TTS_VOICE=en-GB-RyanNeural
JARVIS_USER_ADDRESS=sir
```

Everything else is **optional** and only switches a feature on when its key is present —
JARVIS never crashes or charges money for a missing one. Free-tier keys for extra capacity /
features (all no-credit-card):

| Variable | What it adds | Free key from |
|---|---|---|
| `GROQ_API_KEY_2`, `_3`, … | More Groq quota (round-robin rotation) | console.groq.com/keys |
| `DEEPGRAM_API_KEY` | Better, faster STT (primary ear) | console.deepgram.com |
| `GEMINI_API_KEY` | Vision + chat + embeddings fallback | aistudio.google.com/apikey |
| `CEREBRAS_API_KEY` | Fast chat fallback | cloud.cerebras.ai |
| `TOGETHER_API_KEY` / `MISTRAL_API_KEY` / `OPENROUTER_API_KEY` | More chat/vision fallbacks | each provider's site |
| `TAVILY_API_KEY` | Web search + deep research | tavily.com |
| `ELEVENLABS_API_KEY` | Optional premium voice (Edge-TTS stays the free fallback) | elevenlabs.io |
| `GMAIL_ADDRESS` + `GMAIL_APP_PASSWORD` | Email read/send | myaccount.google.com/apppasswords |
| `GITHUB_TOKEN` | GitHub feed alerts | github.com/settings/tokens |

You can add multiple keys for any provider by suffixing `_2`, `_3`, … — JARVIS rotates
through them to stay under free-tier limits.

### 4. Download the offline wake-word model

The `models/` folder is **not** in the repo (it's large). For the **"Wake up JARVIS"** wake
word, download the small Vosk English model and unzip it into `models/`:

- Get `vosk-model-small-en-us-0.15.zip` from https://alphacephei.com/vosk/models
- Unzip so the path is `models\vosk-model-small-en-us-0.15\`

---

## Run

### Voice listener — the main way to use him

```powershell
.\.venv\Scripts\python.exe scripts\jarvis_listener.py
```

Say **"Wake up JARVIS"** → he greets you → speak your request → he answers aloud. Or in one
breath: **"Wake up JARVIS, what's the weather."** Talk over him to interrupt (barge-in). After
a short quiet pause he returns to wake-word watch. **Use headphones.**

### Backend — HTTP/WS endpoints for the PWA & clients

```powershell
.\.venv\Scripts\python.exe run.py            # serve on 127.0.0.1:8000
.\.venv\Scripts\python.exe run.py --reload   # dev autoreload
```

- `GET  /health`
- `POST /voice/stt` — multipart audio → `{"text": ...}`
- `POST /voice/tts/stream` — `{"text": ...}` → MP3 stream
- `WS   /voice/converse` — full-duplex voice (PCM in, transcript + audio out, barge-in)

---

## Useful tuning knobs (all in `.env`, all optional)

- `EDGE_TTS_VOICE` — voice (default `en-GB-RyanNeural`; try `en-GB-ThomasNeural`)
- `JARVIS_USER_ADDRESS` — what he calls you (default `sir`)
- `WAKE_WORD_THRESHOLD` — wake sensitivity (default `0.5`; lower = easier to trigger)
- `JARVIS_TTS_ENGINE` — `edge` (free default) or `elevenlabs` (only if keys present)
- `JARVIS_STT_ENGINE` — `groq` (default) or local offline `faster-whisper`

The full, commented list of every setting lives in [config.py](config.py).

---

## Notes for collaborators

- **Never commit `.env`.** It's gitignored. Share keys out-of-band, not through the repo.
- Personal files (`MY_PROFILE.md`, `MY_CONTACTS.txt`), `models/`, `database/`, and build
  artifacts are gitignored too — see [.gitignore](.gitignore).
- Read [.claude/RULES.md](.claude/RULES.md) before adding anything: one phase at a time, every
  feature shipped final/production-grade, 100% free forever. Check [STATUS.md](STATUS.md) for
  what's built and what's next.
