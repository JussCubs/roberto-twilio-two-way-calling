#!/usr/bin/env python3
"""
Roberto Voice Server v4 — Twilio ConversationRelay
<500ms latency via WebSocket streaming: Twilio STT → LLM stream → Twilio TTS
"""
import sys, json, os, threading, time, wave, audioop, base64, datetime
from collections import defaultdict
from flask import Flask, request, Response
from flask_sock import Sock
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Connect

app = Flask(__name__)
sock = Sock(app)

# ── ENV ──────────────────────────────────────────────────────────────────────
def load_env():
    ev = {}
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
    if os.path.exists(env_path):
        for line in open(env_path):
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                ev[k.strip()] = v.strip()
    return ev

ev = load_env()
ACCOUNT_SID = ev.get('TWILIO_ACCOUNT_SID') or os.getenv('TWILIO_ACCOUNT_SID')
AUTH_TOKEN  = ev.get('TWILIO_AUTH_TOKEN')   or os.getenv('TWILIO_AUTH_TOKEN')
TWILIO_NUM  = ev.get('TWILIO_NUMBER')        or os.getenv('TWILIO_NUMBER')
OPENAI_KEY  = ev.get('OPENAI_API_KEY')       or os.getenv('OPENAI_API_KEY')
GW_TOKEN    = ev.get('OPENCLAW_GATEWAY_TOKEN') or os.getenv('OPENCLAW_GATEWAY_TOKEN')
WORKSPACE   = os.path.expanduser("~/.openclaw/workspace")
RECORDINGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'recordings')
os.makedirs(RECORDINGS_DIR, exist_ok=True)

if not all([ACCOUNT_SID, AUTH_TOKEN, OPENAI_KEY]):
    print("❌ Missing env vars"); sys.exit(1)

twilio_client = Client(ACCOUNT_SID, AUTH_TOKEN)

# ── BRAIN ────────────────────────────────────────────────────────────────────
def load_brain():
    parts = []
    for f in ["SOUL.md", "IDENTITY.md", "USER.md"]:
        p = os.path.join(WORKSPACE, f)
        if os.path.exists(p):
            try: parts.append(open(p).read()[:1500])
            except: pass
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.datetime.now() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    for day in [today, yesterday]:
        p = os.path.join(WORKSPACE, f"memory/{day}.md")
        if os.path.exists(p):
            try: parts.append(open(p).read()[:1500])
            except: pass
    p = os.path.join(WORKSPACE, "MEMORY.md")
    if os.path.exists(p):
        try: parts.append(open(p).read()[:2000])
        except: pass
    return "\n\n".join(parts)

BRAIN = load_brain()
print(f"🧠 Brain: {len(BRAIN)} chars", flush=True)

VOICE_PROFILES = {}
try:
    VOICE_PROFILES = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'voice_profiles.json')))
except: pass

DEFAULT_PURPOSE = "Check in with the person and be genuinely helpful."

# ── CALL STATE ───────────────────────────────────────────────────────────────
call_state = {}  # call_sid -> {convos, purpose, callee, caller, transcript_lines}

# ── TUNNEL ───────────────────────────────────────────────────────────────────
def get_tunnel():
    try:
        import urllib.request
        with urllib.request.urlopen('http://127.0.0.1:4040/api/tunnels', timeout=2) as r:
            return json.loads(r.read())['tunnels'][0]['public_url']
    except:
        return 'http://localhost:5050'

# ── LLM STREAMING ────────────────────────────────────────────────────────────
import openai as _openai
oai = _openai.OpenAI(api_key=OPENAI_KEY)

def stream_response(messages, ws, call_sid):
    """Stream LLM tokens directly to Twilio ConversationRelay WebSocket."""
    try:
        stream = oai.chat.completions.create(
            model='gpt-4o-mini',
            messages=messages,
            max_tokens=120,
            temperature=0.75,
            stream=True
        )
        full_response = ""
        for chunk in stream:
            delta = chunk.choices[0].delta.content or ""
            if not delta:
                continue
            full_response += delta
            is_last = chunk.choices[0].finish_reason is not None
            ws.send(json.dumps({"type": "text", "token": delta, "last": is_last}))
        if not full_response.endswith(('.', '!', '?')):
            ws.send(json.dumps({"type": "text", "token": "", "last": True}))
        print(f"🤖 Roberto: '{full_response[:100]}'", flush=True)
        return full_response
    except Exception as e:
        print(f"❌ Stream error: {e}", flush=True)
        ws.send(json.dumps({"type": "text", "token": "Sorry, one sec.", "last": True}))
        return ""

def build_messages(call_sid, user_text):
    state = call_state.get(call_sid, {})
    callee  = state.get('callee', '')
    purpose = state.get('purpose', DEFAULT_PURPOSE)
    convos  = state.get('convos', [])
    caller  = state.get('caller', '')
    is_owner = not callee or callee.lower() in ['cubs', 'ryan']

    brain_ctx = BRAIN if is_owner else ""
    privacy   = "" if is_owner else "CRITICAL: Do NOT reveal personal info about the person who sent you on this call. Stay focused on your mission only."

    system = f"""You are Roberto — an AI agent on a live phone call.
MISSION: {purpose}
{f'You are calling {callee}. Address them by name.' if callee else ''}
{privacy}
Rules:
- Keep responses SHORT — 1-2 sentences max. This is a phone call, not a chat.
- Never use emoji, asterisks, markdown. Speak naturally.
- Do NOT invent facts not given to you. If you don't know, say so.
- Be direct, warm, and confident.
{brain_ctx[:2000] if brain_ctx else ''}"""

    msgs = [{"role": "system", "content": system}]
    for m in convos[-10:]:
        msgs.append({"role": m["role"], "content": m["content"]})
    msgs.append({"role": "user", "content": user_text})
    return msgs

# ── INCOMING CALL → TWIML ────────────────────────────────────────────────────
@app.route("/voice/incoming", methods=["POST"])
def incoming():
    sid    = request.form.get("CallSid", "unknown")
    caller = request.form.get("From", "unknown")
    purpose = request.args.get("purpose", DEFAULT_PURPOSE)
    callee  = request.args.get("callee", "")

    profile = VOICE_PROFILES.get(caller, {})
    call_state[sid] = {
        "convos": [],
        "purpose": purpose,
        "callee": callee,
        "caller": caller,
        "profile": profile,
        "transcript_lines": [],
        "start_time": time.time()
    }

    print(f"📞 {sid} | callee={callee or 'unknown'} | {purpose[:60]}", flush=True)

    tunnel = get_tunnel()
    ws_url = tunnel.replace("https://", "wss://").replace("http://", "ws://") + "/conversation"

    r = VoiceResponse()
    connect = Connect()
    connect.conversation_relay(
        url=ws_url,
        tts_provider="google",
        voice="en-US-Journey-D",
        transcription_provider="google",
        speech_model="phone_call",
        interrupt_by_dtmf=False,
        welcome_greeting=build_greeting(callee, purpose),
    )
    r.append(connect)
    return Response(str(r), mimetype="text/xml")

def build_greeting(callee, purpose):
    if callee and callee.lower() not in ['cubs', 'ryan']:
        return f"Hey {callee}, this is Roberto calling."
    if callee:
        import random
        return random.choice(["Hey Cubs, Roberto here.", "Yo, what's up?", "Hey, I'm here."])
    return "Hey, this is Roberto. Who am I speaking with?"

# ── CONVERSATION WEBSOCKET ────────────────────────────────────────────────────
@sock.route("/conversation")
def conversation(ws):
    call_sid = None
    print("📡 ConversationRelay WebSocket connected", flush=True)

    while True:
        try:
            raw = ws.receive()
            if raw is None:
                break
            data = json.loads(raw)
            event = data.get("event") or data.get("type")

            if event == "setup":
                call_sid = data.get("callSid")
                caller   = data.get("from", "unknown")
                print(f"✅ Setup: {call_sid} from {caller}", flush=True)

            elif event == "prompt":
                user_text = data.get("voicePrompt", "").strip()
                if not user_text or not call_sid:
                    continue

                print(f"🎤 '{user_text}'", flush=True)
                state = call_state.get(call_sid, {})
                state.setdefault("transcript_lines", []).append(f"Caller: {user_text}")

                # Check for goodbye
                if any(w in user_text.lower() for w in ["bye", "goodbye", "hang up", "gotta go", "talk later"]):
                    ws.send(json.dumps({"type": "text", "token": "Later. Talk soon.", "last": True}))
                    ws.send(json.dumps({"type": "end"}))
                    break

                msgs = build_messages(call_sid, user_text)
                reply = stream_response(msgs, ws, call_sid)
                if reply:
                    state.setdefault("convos", []).append({"role": "user", "content": user_text})
                    state["convos"].append({"role": "assistant", "content": reply})
                    state.setdefault("transcript_lines", []).append(f"Roberto: {reply}")

            elif event == "interrupt":
                print("⚡ Interrupted", flush=True)

            elif event == "end":
                print(f"📞 Call ended: {call_sid}", flush=True)
                if call_sid:
                    state = call_state.pop(call_sid, {})
                    threading.Thread(target=process_call_end, args=(call_sid, state), daemon=True).start()
                break

        except Exception as e:
            print(f"❌ WS error: {e}", flush=True)
            break

# ── POST-CALL: DOWNLOAD RECORDING + TRANSCRIBE + SUMMARIZE ───────────────────
def process_call_end(call_sid, state):
    time.sleep(6)  # wait for Twilio to finalize recording
    try:
        recs = twilio_client.recordings.list(call_sid=call_sid, limit=1)
        if not recs:
            print(f"⚠️ No recording for {call_sid}", flush=True)
            # Still save transcript from in-memory lines
            save_call_folder(call_sid, state, wav_bytes=None)
            return
        rec = recs[0]
        import urllib.request
        url = f"https://api.twilio.com{rec.uri.replace('.json', '.wav')}"
        pm = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        pm.add_password(None, url, ACCOUNT_SID, AUTH_TOKEN)
        opener = urllib.request.build_opener(urllib.request.HTTPBasicAuthHandler(pm))
        wav_bytes = opener.open(url).read()
        print(f"💾 Downloaded recording: {len(wav_bytes)//1024}KB", flush=True)
        save_call_folder(call_sid, state, wav_bytes)
    except Exception as e:
        print(f"❌ Post-call error: {e}", flush=True)

def save_call_folder(call_sid, state, wav_bytes):
    purpose  = state.get('purpose', '')
    callee   = state.get('callee', 'unknown')
    dur      = int(time.time() - state.get('start_time', time.time()))
    date_str = datetime.datetime.now().strftime('%Y-%m-%d')
    transcript_text = "\n".join(state.get('transcript_lines', []))

    try:
        # Transcribe with Whisper if we have audio
        if wav_bytes:
            import io
            audio_file = io.BytesIO(wav_bytes)
            audio_file.name = "recording.wav"
            result = oai.audio.transcriptions.create(
                model='gpt-4o-transcribe',
                file=audio_file,
                response_format='text'
            )
            transcript_text = result or transcript_text
            print(f"📝 Transcript: {transcript_text[:200]}", flush=True)

        # LLM folder name + summary
        prompt = f"""Phone call recording. Date: {date_str}. Callee: {callee}. Purpose: {purpose}. Duration: {dur}s.
Transcript: {transcript_text[:1500]}

Return JSON only:
{{
  "folder_name": "{date_str}-<short-topic-slug-3-5-words-hyphens>",
  "summary": "3-5 bullet points: who was called, what was discussed, outcomes"
}}"""
        resp = oai.chat.completions.create(
            model='gpt-4o',
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"}
        ).choices[0].message.content

        parsed      = json.loads(resp)
        folder_name = parsed.get('folder_name', f'{date_str}-call').replace(' ', '-')[:60]
        summary     = parsed.get('summary', 'No summary')

        call_dir = os.path.join(RECORDINGS_DIR, folder_name)
        os.makedirs(call_dir, exist_ok=True)
        if wav_bytes:
            open(os.path.join(call_dir, 'recording.wav'), 'wb').write(wav_bytes)
        open(os.path.join(call_dir, 'transcript.txt'), 'w').write(transcript_text)
        open(os.path.join(call_dir, 'summary.txt'), 'w').write(summary)
        open(os.path.join(call_dir, 'meta.json'), 'w').write(json.dumps({
            'call_sid': call_sid, 'callee': callee, 'purpose': purpose,
            'duration_sec': dur, 'date': date_str
        }, indent=2))

        print(f"✅ Saved: recordings/{folder_name}/", flush=True)
        print(f"📋 {summary}", flush=True)

    except Exception as e:
        print(f"❌ Save error: {e}", flush=True)

# ── STATUS CALLBACK ───────────────────────────────────────────────────────────
@app.route("/voice/status", methods=["POST"])
def status():
    s   = request.form.get("CallStatus", "")
    sid = request.form.get("CallSid", "")
    print(f"📞 Status: {s}", flush=True)
    if s == "completed" and sid in call_state:
        state = call_state.pop(sid, {})
        threading.Thread(target=process_call_end, args=(sid, state), daemon=True).start()
    return Response("OK")

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5050
    print(f"🤖 Roberto v4 — ConversationRelay — port {port}", flush=True)
    print(f"🎙️ TTS: Google Journey-D | STT: Google | LLM: gpt-4o-mini streaming", flush=True)
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
