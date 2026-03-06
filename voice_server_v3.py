#!/usr/bin/env python3
"""Roberto Voice Server — Kokoro TTS + Grok front brain + OpenClaw deep brain"""
import sys, json, subprocess, threading, time, os, random, hashlib, io, struct, asyncio
from collections import defaultdict
from datetime import datetime, timedelta
from flask import Flask, request, Response
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Gather
import numpy as np

app = Flask(__name__)

# ── ENV ──────────────────────────────────────────────────────────────────────
def load_env():
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
    ev = {}
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
XAI_KEY     = ev.get('XAI_API_KEY')          or os.getenv('XAI_API_KEY')
GW_TOKEN    = ev.get('OPENCLAW_GATEWAY_TOKEN') or os.getenv('OPENCLAW_GATEWAY_TOKEN')
GW_PORT     = int(ev.get('GATEWAY_PORT', '18789'))
WORKSPACE   = os.path.expanduser("~/.openclaw/workspace")

if not all([ACCOUNT_SID, AUTH_TOKEN, XAI_KEY, GW_TOKEN]):
    print("❌ Missing env vars"); sys.exit(1)

client = Client(ACCOUNT_SID, AUTH_TOKEN)

# ── KOKORO TTS ───────────────────────────────────────────────────────────────
_kokoro = None
_tts_cache = {}  # token -> text

def get_kokoro():
    global _kokoro
    if _kokoro is None:
        d = os.path.dirname(os.path.abspath(__file__))
        _kokoro = Kokoro(os.path.join(d, 'kokoro-v1.0.onnx'), os.path.join(d, 'voices-v1.0.bin'))
    return _kokoro

def get_tunnel():
    try:
        import urllib.request
        with urllib.request.urlopen('http://127.0.0.1:4040/api/tunnels', timeout=2) as r:
            return json.loads(r.read())['tunnels'][0]['public_url']
    except:
        return 'http://localhost:5050'

def wav_header(sr=24000):
    h  = struct.pack('<4sI4s', b'RIFF', 0xFFFFFFFF, b'WAVE')
    h += struct.pack('<4sIHHIIHH', b'fmt ', 16, 1, 1, sr, sr*2, 2, 16)
    h += struct.pack('<4sI', b'data', 0xFFFFFFFF)
    return h

_tts_mp3_cache = {}  # token -> mp3 bytes (pre-generated)

@app.route("/tts/<token>")
def serve_tts(token):
    # Serve pre-generated if available
    if token in _tts_mp3_cache:
        return Response(_tts_mp3_cache.pop(token), mimetype='audio/mpeg', headers={'Cache-Control': 'no-store'})
    cached = _tts_cache.get(token)
    if not cached:
        return Response("Not found", status=404)
    text, voice = cached
    import openai as _openai
    oai = _openai.OpenAI(api_key=ev.get('OPENAI_API_KEY') or os.getenv('OPENAI_API_KEY'))
    audio = oai.audio.speech.create(model='tts-1', voice=voice, input=text, response_format='mp3')
    return Response(audio.content, mimetype='audio/mpeg', headers={'Cache-Control': 'no-store'})

def ksay(obj, text, voice='onyx'):
    token = hashlib.md5((text+voice).encode()).hexdigest()[:12]
    _tts_cache[token] = (text, voice)
    url = f"{get_tunnel()}/tts/{token}"
    obj.play(url)
    print(f"🔊 [openai:{voice}] '{text[:60]}'", flush=True)

# ── BRAIN ────────────────────────────────────────────────────────────────────
def load_brain():
    parts = []
    for f in ["SOUL.md", "IDENTITY.md", "USER.md"]:
        p = os.path.join(WORKSPACE, f)
        if os.path.exists(p):
            try: parts.append(open(p).read()[:2000])
            except: pass
    today = datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.now()-timedelta(days=1)).strftime("%Y-%m-%d")
    for day in [today, yesterday]:
        p = os.path.join(WORKSPACE, f"memory/{day}.md")
        if os.path.exists(p):
            try: parts.append(f"=== {day} ===\n"+open(p).read()[:2000])
            except: pass
    p = os.path.join(WORKSPACE, "MEMORY.md")
    if os.path.exists(p):
        try: parts.append("=== MEMORY ===\n"+open(p).read()[:3000])
        except: pass
    return "\n\n".join(parts)

BRAIN = load_brain()
print(f"🧠 Brain: {len(BRAIN)} chars", flush=True)

# ── CALL STATE ───────────────────────────────────────────────────────────────
convos      = defaultdict(list)
deep_q      = {}
deep_busy   = {}
silence_ct  = defaultdict(int)
purposes    = {}
profiles_c  = {}
DEFAULT_PURPOSE = "Check in with Ryan and be genuinely helpful."

VOICE_PROFILES = {}
try:
    VOICE_PROFILES = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'voice_profiles.json')))
except: pass
print(f"📱 {len(VOICE_PROFILES)} profiles", flush=True)

# ── GROK FRONT BRAIN ─────────────────────────────────────────────────────────
def grok(messages, max_tokens=100):
    try:
        result = subprocess.run([
            "curl", "-s", "--max-time", "7",
            "https://api.x.ai/v1/chat/completions",
            "-H", "Content-Type: application/json",
            "-H", f"Authorization: Bearer {XAI_KEY}",
            "-d", json.dumps({"model":"grok-3-fast-beta","max_tokens":max_tokens,"temperature":0.75,"messages":messages})
        ], capture_output=True, text=True, timeout=9)
        return json.loads(result.stdout)["choices"][0]["message"]["content"].strip('"\'')
    except Exception as e:
        print(f"❌ Grok: {e}", flush=True)
        return None

# ── DEEP BRAIN ───────────────────────────────────────────────────────────────
def deep_brain(call_sid, msg, history, purpose):
    deep_busy[call_sid] = True
    try:
        ctx = "\n".join([f"{'Ryan' if m['role']=='user' else 'Roberto'}: {m['content']}" for m in history[-6:]])
        prompt = f"Ryan is on a LIVE PHONE CALL.\nMISSION: {purpose}\nHe said: \"{msg}\"\nRecent:\n{ctx}\n\nRespond 1-3 sentences, spoken aloud, no emoji, stay on mission."
        result = subprocess.run([
            "curl","-s","--max-time","25","-X","POST",
            f"http://localhost:{GW_PORT}/api/v1/sessions/spawn",
            "-H",f"Authorization: Bearer {GW_TOKEN}",
            "-H","Content-Type: application/json",
            "-d",json.dumps({"task":prompt,"runTimeoutSeconds":20})
        ], capture_output=True, text=True, timeout=30)
        if result.stdout.strip():
            data = json.loads(result.stdout)
            deep_q[call_sid] = data.get("result") or data.get("message","")
    except Exception as e:
        print(f"❌ Deep: {e}", flush=True)
    finally:
        deep_busy[call_sid] = False

# ── RESPONSE ─────────────────────────────────────────────────────────────────
def respond(msg, call_sid):
    h = convos[call_sid]
    h.append({"role":"user","content":msg})
    purpose = purposes.get(call_sid, DEFAULT_PURPOSE)

    if call_sid in deep_q:
        dr = deep_q.pop(call_sid)
        r = grok([
            {"role":"system","content":f"Roberto on a call. Got results. Weave in naturally, 2 sentences. Mission: {purpose}"},
            {"role":"user","content":f"Ryan: {msg}\nResult: {dr}\nSpeak:"}
        ], 120) or dr[:200]
        h.append({"role":"assistant","content":r}); return r

    task_words = ["check","find","cancel","look up","handle","fix","unsubscribe","book","what's","how much","search","email","remind"]
    if any(w in msg.lower() for w in task_words):
        threading.Thread(target=deep_brain, args=(call_sid,msg,list(h),purpose), daemon=True).start()
        r = grok([
            {"role":"system","content":f"Roberto on a call. Ryan needs something done. 1 sentence acknowledgment. Mission: {purpose}"},
            {"role":"user","content":f"Ryan: {msg}\nAcknowledge:"}
        ], 40) or "On it."
        h.append({"role":"assistant","content":r}); return r

    msgs = [{"role":"system","content":f"You are Roberto — Ryan's personal AI, on a live phone call. Direct, witty, competent. Max 2 sentences spoken aloud. No emoji.\nMISSION: {purpose}\n\nCONTEXT:\n{BRAIN}"}]
    for m in h[-12:]:
        msgs.append({"role":"user" if m["role"]=="user" else "assistant","content":m["content"]})
    r = grok(msgs, 100) or "Still here — what's on your mind?"
    h.append({"role":"assistant","content":r}); return r

# ── ROUTES ───────────────────────────────────────────────────────────────────
def make_gather(timeout=7):
    return Gather(input="speech", action="/voice/respond", timeout=timeout, speech_timeout="1", language="en-US")

@app.route("/voice/incoming", methods=["POST"])
def incoming():
    sid     = request.form.get("CallSid","unknown")
    caller  = request.form.get("From","unknown")
    purpose = request.args.get("purpose", DEFAULT_PURPOSE)
    callee  = request.args.get("callee", "")  # name of person being called

    convos[sid] = []; silence_ct[sid] = 0; purposes[sid] = purpose
    profile = VOICE_PROFILES.get(caller)
    profiles_c[sid] = {"number":caller,"profile":profile,"callee":callee}

    print(f"📞 {sid} | callee={callee or 'unknown'} | {purpose[:60]}", flush=True)

    if callee:
        # Seed convo so Roberto knows who he is talking to
        convos[sid].append({"role":"system","content":f"You are calling {callee}. Address them by name. Your mission: {purpose}"})
        greeting = f"Hey {callee}, this is Roberto calling. {purpose}"
    else:
        convos[sid].append({"role":"system","content":f"You called someone. Get their name first, then: {purpose}"})
        greeting = "Hey, this is Roberto. Who am I speaking with?"

    r = VoiceResponse()
    r.pause(length=1)
    g = make_gather(); ksay(g, greeting); r.append(g)
    r.redirect("/voice/reprompt")
    return Response(str(r), mimetype="text/xml")

import random as _random
_FILLERS = ["Mm.", "Hmm.", "Yeah.", "Mm-hmm.", "Uh-huh.", "Got it.", "Okay.", "Sure.", "Right."]
_pending_replies = {}  # sid -> mp3 bytes or None

@app.route("/voice/respond", methods=["POST"])
def voice_respond():
    sid    = request.form.get("CallSid","unknown")
    speech = request.form.get("SpeechResult","").strip()
    silence_ct[sid] = 0
    print(f"🎤 '{speech}'", flush=True)

    if not speech:
        r = VoiceResponse(); r.redirect("/voice/reprompt"); return Response(str(r), mimetype="text/xml")

    if any(w in speech.lower() for w in ["bye","goodbye","hang up","gotta go","later","talk later"]):
        r = VoiceResponse(); ksay(r, "Later. Talk soon."); r.hangup()
        return Response(str(r), mimetype="text/xml")

    # Kick off LLM + TTS in background immediately
    _pending_replies[sid] = None
    threading.Thread(target=_generate_reply, args=(sid, speech), daemon=True).start()

    # Return filler instantly so caller hears something while we generate
    filler = _random.choice(_FILLERS)
    r = VoiceResponse()
    r.say(filler, voice="Google.en-US-Journey-D")
    r.redirect(f"/voice/ready/{sid}")
    return Response(str(r), mimetype="text/xml")

def _generate_reply(sid, speech):
    reply = respond(speech, sid)
    print(f"🤖 Roberto: '{reply}'", flush=True)
    # Pre-generate TTS — runs in parallel while filler plays
    try:
        import openai as _openai
        oai = _openai.OpenAI(api_key=ev.get('OPENAI_API_KEY') or os.getenv('OPENAI_API_KEY'))
        audio = oai.audio.speech.create(model='tts-1', voice='onyx', input=reply, response_format='mp3')
        token = hashlib.md5(reply.encode()).hexdigest()[:12]
        _tts_cache[token] = (reply, 'onyx')
        _tts_mp3_cache[token] = audio.content
        _pending_replies[sid] = token
        print(f"✅ TTS ready: {token}", flush=True)
    except Exception as e:
        print(f"❌ TTS gen: {e}", flush=True)
        _pending_replies[sid] = f"__say__{reply}"

@app.route("/voice/ready/<sid>", methods=["POST"])
def voice_ready(sid):
    # Poll up to 8s for the reply to be ready
    for _ in range(40):
        val = _pending_replies.get(sid)
        if val is not None:
            break
        time.sleep(0.2)

    _pending_replies.pop(sid, None)
    r = VoiceResponse()
    g = make_gather()

    if val and val.startswith("__say__"):
        r.say(val[7:], voice="Google.en-US-Journey-D")
    elif val:
        token = val
        url = f"{get_tunnel()}/tts/{token}"
        g.play(url)
    else:
        g.play(get_tunnel() + "/tts/fallback")
        r.say("Sorry, give me one more second.", voice="Google.en-US-Journey-D")

    r.append(g)
    r.redirect("/voice/reprompt")
    return Response(str(r), mimetype="text/xml")

@app.route("/voice/reprompt", methods=["POST"])
def reprompt():
    sid = request.form.get("CallSid","unknown")
    cnt = silence_ct[sid] = silence_ct.get(sid,0) + 1
    purpose = purposes.get(sid, DEFAULT_PURPOSE)

    if sid in deep_q:
        dr = deep_q.pop(sid)
        reply = grok([
            {"role":"system","content":f"Roberto on call. Results in. Share naturally 2-3 sentences. Mission: {purpose}"},
            {"role":"user","content":f"Results: {dr}\nSpeak:"}
        ], 120) or dr[:200]
        convos[sid].append({"role":"assistant","content":reply})
        r = VoiceResponse(); g = make_gather(timeout=8); ksay(g, reply); r.append(g)
        r.redirect("/voice/reprompt"); return Response(str(r), mimetype="text/xml")

    if deep_busy.get(sid):
        filler = ["Still working on that, one sec.", "Almost there.", "Hold on, pulling that up."][min(cnt-1,2)]
        r = VoiceResponse(); g = make_gather(timeout=5); ksay(g, filler); r.append(g)
        r.redirect("/voice/reprompt"); return Response(str(r), mimetype="text/xml")

    ctx = "\n".join([f"{'Ryan' if m['role']=='user' else 'Roberto'}: {m['content']}" for m in convos.get(sid,[])[-6:]])
    filler = grok([
        {"role":"system","content":f"Roberto on call, Ryan went quiet. Add insight/follow-up. 1-2 sentences. Mission: {purpose}\nContext: {BRAIN[:600]}"},
        {"role":"user","content":f"Recent:\n{ctx}\n\nRyan quiet. Say something useful:"}
    ], 80) or "Still here whenever you're ready."
    convos[sid].append({"role":"assistant","content":filler})
    print(f"🔇 Silence #{cnt}: '{filler}'", flush=True)
    r = VoiceResponse(); g = make_gather(timeout=10); ksay(g, filler); r.append(g)
    r.redirect("/voice/reprompt"); return Response(str(r), mimetype="text/xml")

@app.route("/voice/status", methods=["POST"])
def status():
    s, sid = request.form.get("CallStatus",""), request.form.get("CallSid","")
    if s in ("completed","failed","busy","no-answer"):
        print(f"📞 {s}", flush=True)
        meta = {**profiles_c.get(sid,{}), 'purpose': purposes.get(sid,''), 'call_sid': sid}
        for d in [convos,deep_q,deep_busy,silence_ct,purposes,profiles_c]: d.pop(sid,None)
        if s == "completed":
            threading.Thread(target=fetch_and_process_recording, args=(sid, meta), daemon=True).start()
    return Response("OK")

def fetch_and_process_recording(call_sid, meta):
    """Download Twilio's native recording (high quality) after call ends."""
    import time as _time
    _time.sleep(5)  # give Twilio a moment to finalize
    try:
        recordings = client.recordings.list(call_sid=call_sid, limit=1)
        if not recordings:
            print(f"⚠️ No Twilio recording found for {call_sid}", flush=True)
            return
        rec = recordings[0]
        # Download as WAV (8kHz stereo from Twilio — much cleaner than our WebSocket capture)
        url = f"https://api.twilio.com{rec.uri.replace('.json','.wav')}"
        import urllib.request
        pw_mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        pw_mgr.add_password(None, url, ACCOUNT_SID, AUTH_TOKEN)
        handler = urllib.request.HTTPBasicAuthHandler(pw_mgr)
        opener = urllib.request.build_opener(handler)
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        tmp_wav = os.path.join(RECORDINGS_DIR, f"{ts}_{call_sid[:8]}.wav")
        with opener.open(url) as r:
            open(tmp_wav, 'wb').write(r.read())
        print(f"💾 Downloaded Twilio recording: {tmp_wav}", flush=True)
        transcribe_call(tmp_wav, meta)
    except Exception as e:
        print(f"❌ Recording fetch error: {e}", flush=True)

# ─── CALL MONITOR (Live audio + recording + transcription) ───────────────────
import audioop, wave, base64, datetime
import sounddevice as sd
from flask_sock import Sock

sock = Sock(app)
_out_stream = None
_call_recordings = {}  # call_sid -> {wav_writer, wav_path, start_time}
RECORDINGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'recordings')
os.makedirs(RECORDINGS_DIR, exist_ok=True)

def play_mulaw(mulaw_bytes):
    pass  # playback removed — record only

@sock.route('/monitor/stream')
def media_stream(ws):
    call_sid = None
    print("📡 Twilio Media Stream connected", flush=True)
    while True:
        try:
            raw = ws.receive()
            if raw is None: break
            data = json.loads(raw)
            event = data.get('event')

            if event == 'start':
                call_sid = data['start']['callSid']
                ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                wav_path = os.path.join(RECORDINGS_DIR, f"{ts}_{call_sid[:8]}.wav")
                wf = wave.open(wav_path, 'wb')
                wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(8000)
                _call_recordings[call_sid] = {'wav_path': wav_path, 'wav_writer': wf, 'start_time': time.time()}
                print(f"🔴 Recording: {wav_path}", flush=True)

            elif event == 'media' and call_sid:
                mulaw = base64.b64decode(data['media']['payload'])
                play_mulaw(mulaw)
                rec = _call_recordings.get(call_sid)
                if rec:
                    pcm = audioop.ulaw2lin(mulaw, 2)
                    rec['wav_writer'].writeframes(pcm)

            elif event == 'stop' and call_sid:
                rec = _call_recordings.pop(call_sid, None)
                if rec:
                    rec['wav_writer'].close()
                    dur = time.time() - rec['start_time']
                    print(f"💾 Saved {rec['wav_path']} ({dur:.0f}s)", flush=True)
                    meta = profiles_c.get(call_sid, {})
                    meta['purpose'] = purposes.get(call_sid, '')
                    meta['duration_sec'] = int(dur)
                    threading.Thread(target=transcribe_call, args=(rec['wav_path'], meta), daemon=True).start()
                break
        except Exception as e:
            print(f"❌ Stream: {e}", flush=True)
            break

def transcribe_call(wav_path, call_meta=None):
    print(f"📝 Transcribing...", flush=True)
    try:
        import openai as _openai
        oai = _openai.OpenAI(api_key=ev.get('OPENAI_API_KEY') or os.getenv('OPENAI_API_KEY'))

        with open(wav_path, 'rb') as f:
            transcript = oai.audio.transcriptions.create(model='gpt-4o-transcribe', file=f, response_format='text')

        if not transcript.strip():
            transcript = "[No speech detected]"

        print(f"📝 Transcript: {transcript[:200]}", flush=True)

        callee  = (call_meta or {}).get('callee', 'unknown')
        purpose = (call_meta or {}).get('purpose', '')
        dur     = (call_meta or {}).get('duration_sec', 0)
        date    = datetime.datetime.now().strftime('%Y-%m-%d')

        prompt = f"""You are organizing AI phone call recordings.
Call metadata: callee={callee}, purpose={purpose}, duration={dur}s, date={date}
Transcript: {transcript}

Return valid JSON only (no markdown):
{{
  "folder_name": "<{date}>-<topic-slug-max-5-words-hyphens>",
  "summary": "bullet point summary of the call, 3-5 points"
}}"""

        resp = oai.chat.completions.create(
            model='gpt-4o',
            messages=[{"role":"user","content":prompt}],
            response_format={"type":"json_object"}
        ).choices[0].message.content

        parsed = json.loads(resp)
        folder_name = parsed.get('folder_name', f'{date}-call').replace(' ', '-')[:60]
        summary     = parsed.get('summary', 'No summary')

        # Organize into named folder
        call_dir = os.path.join(RECORDINGS_DIR, folder_name)
        os.makedirs(call_dir, exist_ok=True)
        os.rename(wav_path, os.path.join(call_dir, 'recording.wav'))
        open(os.path.join(call_dir, 'transcript.txt'), 'w').write(transcript)
        open(os.path.join(call_dir, 'summary.txt'), 'w').write(summary if isinstance(summary, str) else '\n'.join(summary))
        open(os.path.join(call_dir, 'meta.json'), 'w').write(json.dumps(call_meta or {}, indent=2))

        print(f"✅ recordings/{folder_name}/", flush=True)
        print(f"📋 {summary}", flush=True)

    except Exception as e:
        print(f"❌ Transcription error: {e}", flush=True)



if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5050
    print(f"🤖 Roberto Voice Server on port {port}", flush=True)
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)

