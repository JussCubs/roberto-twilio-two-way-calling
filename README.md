# Roberto — Two-Way AI Phone Calls via Twilio

A two-way conversational AI phone call system powered by:
- **Twilio** — phone calls + media streaming
- **OpenAI TTS** (`onyx` voice) — natural speech synthesis
- **Grok 3 Fast** (xAI) — instant front-brain responses
- **OpenClaw Gateway** — deep memory/tools brain
- **Whisper** — post-call transcription
- **GPT-4o-mini** — call summary

## Features
- 🎙️ Two-way live conversation with AI (Roberto)
- 🎯 Purpose/goal system — give Roberto a mission per call
- 👤 Callee system — address specific people by name
- 🔒 Privacy — no personal context leaks to third parties
- 🔴 Live audio monitoring — hear both sides on your Mac
- 💾 Call recording (WAV)
- 📝 Auto-transcription + summary after hangup

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# Fill in your credentials

# Start ngrok tunnel
ngrok http 5050

# Start server
python3 voice_server_v3.py 5050
```

## Making a Call

```python
from twilio.rest import Client
import urllib.parse

client = Client(ACCOUNT_SID, AUTH_TOKEN)
callee = "Swerve"
purpose = "Tell Swerve that Novak was diagnosed with Tourettes."

url = f"{NGROK_URL}/voice/incoming?callee={urllib.parse.quote(callee)}&purpose={urllib.parse.quote(purpose)}"
call = client.calls.create(to="+13128718126", from_=TWILIO_NUMBER, url=url, method="POST")
```

## Voice Profiles

Edit `voice_profiles.json` to add known callers:
```json
{
  "+13128718126": {
    "name": "Cubs",
    "relationship": "human partner",
    "context": "Building robertoagent.com"
  }
}
```

## Architecture

```
Incoming call → /voice/incoming (TwiML + Media Stream)
                ↓
          /monitor/stream (WebSocket)
          ├── Live audio → Mac speakers
          └── WAV recording → recordings/

Speech → /voice/respond
         ├── Grok 3 Fast (instant reply)
         └── OpenClaw deep brain (async tasks)
              ↓
         OpenAI TTS (onyx) → /tts/<token> → Twilio <Play>

Hangup → Whisper transcription → GPT summary → Telegram notification
```
