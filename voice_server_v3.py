#!/usr/bin/env python3
"""Roberto Voice Server — Kokoro TTS + Grok front brain + OpenClaw deep brain"""
import sys, json, subprocess, threading, time, os, random, hashlib, io, struct, asyncio
from collections import defaultdict
from datetime import datetime, timedelta
from flask import Flask, request, Response
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Gather
import numpy as np
import soundfile as sf
from kokoro_onnx import Kokoro

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

@app.route("/tts/<token>")
def serve_tts(token):
    cached = _tts_cache.get(token)
    if not cached:
        return Response("Not found", status=404)
    text, voice = cached
    import openai as _openai
    oai_key = ev.get('OPENAI_API_KEY') or os.getenv('OPENAI_API_KEY')
    oai = _openai.OpenAI(api_key=oai_key)
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
    return Gather(input="speech", action="/voice/respond", timeout=timeout, speech_timeout="auto", language="en-US")

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

    tunnel = get_tunnel()
    ws_url = tunnel.replace("https://","wss://").replace("http://","ws://") + "/monitor/stream"

    from twilio.twiml.voice_response import Connect, Stream
    r = VoiceResponse()
    # Attach media stream for live monitoring + recording
    connect = Connect()
    connect.stream(url=ws_url)
    r.append(connect)
    r.pause(length=1)
    g = make_gather(); ksay(g, greeting); r.append(g)
    r.redirect("/voice/reprompt")
    return Response(str(r), mimetype="text/xml")

@app.route("/voice/respond", methods=["POST"])
def voice_respond():
    sid    = request.form.get("CallSid","unknown")
    speech = request.form.get("SpeechResult","").strip()
    silence_ct[sid] = 0
    print(f"🎤 Ryan: '{speech}'", flush=True)

    if not speech:
        r = VoiceResponse(); r.redirect("/voice/reprompt"); return Response(str(r), mimetype="text/xml")

    if any(w in speech.lower() for w in ["bye","goodbye","hang up","gotta go","later","talk later"]):
        r = VoiceResponse(); ksay(r, "Later. I'll keep things running."); r.hangup()
        return Response(str(r), mimetype="text/xml")

    reply = respond(speech, sid)
    print(f"🤖 Roberto: '{reply}'", flush=True)
    r = VoiceResponse(); g = make_gather(); ksay(g, reply); r.append(g)
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
        for d in [convos,deep_q,deep_busy,silence_ct,purposes,profiles_c]: d.pop(sid,None)
    return Response("OK")

# ─── CALL MONITOR (Live audio + recording + transcription) ───────────────────
import audioop, wave, base64, datetime
import sounddevice as sd
from flask_sock import Sock

sock = Sock(app)
_out_stream = None
_call_recordings = {}  # call_sid -> {wav_writer, wav_path, start_time}
RECORDINGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'recordings')
os.makedirs(RECORDINGS_DIR, exist_ok=True)

import queue as _queue
_audio_q = _queue.Queue(maxsize=200)

def _audio_player():
    stream = sd.OutputStream(samplerate=24000, channels=1, dtype='int16', blocksize=256)
    stream.start()
    while True:
        try:
            chunk = _audio_q.get(timeout=1)
            stream.write(chunk)
        except _queue.Empty:
            pass
        except Exception as e:
            print(f"Audio player: {e}", flush=True)

threading.Thread(target=_audio_player, daemon=True).start()

def play_mulaw(mulaw_bytes):
    try:
        pcm_8k = audioop.ulaw2lin(mulaw_bytes, 2)
        pcm_24k, _ = audioop.ratecv(pcm_8k, 2, 1, 8000, 24000, None)
        arr = np.frombuffer(pcm_24k, dtype=np.int16).copy()
        try: _audio_q.put_nowait(arr)
        except _queue.Full: pass
    except: pass

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
                    threading.Thread(target=transcribe_call, args=(rec['wav_path'],), daemon=True).start()
                break
        except Exception as e:
            print(f"❌ Stream: {e}", flush=True)
            break

def transcribe_call(wav_path):
    print(f"📝 Transcribing {wav_path}...", flush=True)
    try:
        import openai as _openai
        oai = _openai.OpenAI(api_key=ev.get('OPENAI_API_KEY') or os.getenv('OPENAI_API_KEY'))
        with open(wav_path, 'rb') as f:
            transcript = oai.audio.transcriptions.create(model='whisper-1', file=f, response_format='text')
        print(f"📝 Transcript:\n{transcript}", flush=True)

        summary = oai.chat.completions.create(
            model='gpt-4o-mini',
            messages=[
                {"role":"system","content":"Summarize this phone call in 3-5 bullet points: who was called, what was discussed, outcomes."},
                {"role":"user","content":transcript}
            ]
        ).choices[0].message.content
        print(f"\n📋 Summary:\n{summary}", flush=True)

        base = wav_path.replace('.wav','')
        open(f"{base}_transcript.txt",'w').write(transcript)
        open(f"{base}_summary.txt",'w').write(summary)
        print(f"✅ Saved transcript + summary", flush=True)
    except Exception as e:
        print(f"❌ Transcription error: {e}", flush=True)



if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5050
    print(f"🤖 Roberto Voice Server on port {port}", flush=True)
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)

