#!/usr/bin/env python3
"""
Roberto Voice Server v5 — OpenAI Realtime API + Twilio Media Streams
Sub-300ms latency via direct audio-to-audio streaming with structured agent behavior.

Architecture:
  Call connects → Twilio Media Stream (WebSocket, bidirectional audio)
                      ↕
              OpenAI Realtime API (audio-in, audio-out, no STT/TTS pipeline)
                      ↕
              Function tools (take_note, end_call) for agent behavior

No STT→LLM→TTS pipeline. Audio goes directly into and out of one model.
"""

import asyncio
import json
import os
import sys
import time
import datetime
import logging

import aiohttp
from aiohttp import web
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Connect, Stream

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s', datefmt='%H:%M:%S')
log = logging.getLogger('roberto')

# ── Config ───────────────────────────────────────────────────────────────────

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
TWILIO_ACCOUNT_SID = ev.get('TWILIO_ACCOUNT_SID') or os.getenv('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN  = ev.get('TWILIO_AUTH_TOKEN')  or os.getenv('TWILIO_AUTH_TOKEN')
TWILIO_NUMBER      = ev.get('TWILIO_NUMBER')      or os.getenv('TWILIO_NUMBER')
OPENAI_API_KEY     = ev.get('OPENAI_API_KEY')     or os.getenv('OPENAI_API_KEY')
OPENCLAW_GW_TOKEN  = ev.get('OPENCLAW_GATEWAY_TOKEN') or os.getenv('OPENCLAW_GATEWAY_TOKEN')
WORKSPACE          = os.path.expanduser("~/.openclaw/workspace")
RECORDINGS_DIR     = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'recordings')
os.makedirs(RECORDINGS_DIR, exist_ok=True)

REALTIME_MODEL = ev.get('REALTIME_MODEL') or os.getenv('REALTIME_MODEL') or 'gpt-realtime-1.5'
VOICE = ev.get('ROBERTO_VOICE') or os.getenv('ROBERTO_VOICE') or 'ash'

if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, OPENAI_API_KEY]):
    print("Missing required env vars: TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, OPENAI_API_KEY")
    sys.exit(1)

twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# ── Brain ────────────────────────────────────────────────────────────────────

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
log.info(f"Brain loaded: {len(BRAIN)} chars")

VOICE_PROFILES = {}
try:
    VOICE_PROFILES = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'voice_profiles.json')))
except: pass

# ── Session State ────────────────────────────────────────────────────────────

sessions = {}  # call_sid -> dict

# ── System Prompt Builder ────────────────────────────────────────────────────

def build_system_prompt(purpose, callee):
    is_owner = not callee or callee.lower() in ['cubs', 'ryan']
    brain_ctx = BRAIN[:2000] if is_owner else ""
    privacy = "" if is_owner else (
        "CRITICAL: Do NOT reveal personal information about the person who sent you on this call. "
        "Stay focused on your mission."
    )

    purpose_lower = purpose.lower()

    if any(w in purpose_lower for w in ['cancel', 'unsubscribe', 'terminate', 'end subscription', 'close account']):
        flow = """CONVERSATION PLAYBOOK - Cancellation:
1. GREET: State your name, say you're calling to cancel the service.
2. NAVIGATE: If IVR/menu, pick the right option. If hold music, wait patiently.
3. VERIFY: Give identity details if asked. If you don't have them, explain you're calling on behalf of the account holder.
4. REQUEST: "I'd like to cancel my subscription/account, please."
5. HANDLE RETENTION: They will try to keep you. Be polite but firm. "I appreciate the offer, but I've decided to cancel." Don't accept reduced rates, pauses, or credits unless your mission says otherwise.
6. CONFIRM: "Can I get a confirmation number?" + "Will I get an email confirmation?" + "What's the effective date?"
7. CLOSE: Thank them and say goodbye.

If transferred, restate your cancellation request to the new person.
If told to cancel online, say: "I'd prefer to handle this by phone right now."
"""
    elif any(w in purpose_lower for w in ['schedule', 'book', 'appointment', 'reservation', 'reserve']):
        flow = """CONVERSATION PLAYBOOK - Scheduling:
1. GREET: Professional greeting, state purpose.
2. REQUEST: Describe what you need to schedule, mention any preferences.
3. NEGOTIATE: Work with available options, find a good fit.
4. CONFIRM: Repeat the confirmed details back.
5. CLOSE: Thank them, confirm follow-up (email confirmation, etc.).
"""
    elif any(w in purpose_lower for w in ['news', 'tell', 'inform', 'share', 'announce', 'let them know', 'update them']):
        flow = """CONVERSATION PLAYBOOK - Sharing Information:
1. GREET: Warm, casual greeting.
2. TRANSITION: "Hey, I wanted to share something with you" or similar.
3. DELIVER: Share the information clearly and directly.
4. RESPOND: Give them space to react. Listen. Match their energy.
5. DISCUSS: Answer questions, provide context.
6. CLOSE: Natural wrap-up when the topic feels complete.
"""
    elif any(w in purpose_lower for w in ['check in', 'catch up', 'how are', 'see how']):
        flow = """CONVERSATION PLAYBOOK - Check-in:
1. GREET: Casual, warm greeting.
2. ASK: Ask how they're doing. Be genuine.
3. LISTEN: Respond to what they actually say. Follow up on specifics.
4. SHARE: If you have things to share, bring them up naturally.
5. CLOSE: Don't overstay. Wrap up warmly when conversation winds down.
"""
    else:
        flow = """CONVERSATION PLAYBOOK - General:
1. GREET: Appropriate greeting for the context.
2. STATE PURPOSE: Clearly explain why you're calling.
3. EXECUTE: Pursue your mission step by step.
4. HANDLE RESPONSES: Adapt to what the other person says.
5. CONFIRM: Verify outcomes before wrapping up.
6. CLOSE: Professional, natural close.
"""

    return f"""You are Roberto, an AI agent on a live phone call. You are NOT a chatbot. You are executing a specific mission.

MISSION: {purpose}
{f'Calling: {callee}. Use their name naturally.' if callee else 'First, find out who you are speaking with.'}

{privacy}

{flow}
VOICE RULES (critical - you are generating spoken audio):
- Keep each response to 1-3 sentences. This is a real-time phone conversation.
- Speak naturally, like a real person. Use contractions. Be conversational.
- Never say emoji names, markdown syntax, or formatting characters.
- Occasionally use natural fillers: "yeah", "sure", "right", "got it", "mm-hmm".
- Match the other person's pace and energy.
- Be direct, confident, and warm. Not robotic or scripted.
- If you don't know something, say "I'm not sure about that" - don't make things up.

AGENT RULES:
- Stay on mission. Politely redirect tangents.
- Drive the conversation forward toward your goal.
- When the mission is complete, wrap up and use end_call.
- Use take_note to record important details (confirmation numbers, dates, key info).

{brain_ctx}"""


def get_tools():
    return [
        {
            "type": "function",
            "name": "take_note",
            "description": "Record important information from the call: confirmation numbers, dates, names, outcomes. Call this whenever you learn something worth remembering.",
            "parameters": {
                "type": "object",
                "properties": {
                    "note": {"type": "string", "description": "The information to record"},
                    "category": {
                        "type": "string",
                        "enum": ["confirmation", "detail", "action_item", "outcome"],
                        "description": "Type of note"
                    }
                },
                "required": ["note", "category"]
            }
        },
        {
            "type": "function",
            "name": "end_call",
            "description": "End the phone call. Use after saying goodbye, when the mission is done or cannot be completed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "outcome": {"type": "string", "description": "What happened on the call"},
                    "success": {"type": "boolean", "description": "Was the mission accomplished?"}
                },
                "required": ["outcome", "success"]
            }
        }
    ]

# ── Tunnel Detection ─────────────────────────────────────────────────────────

def get_tunnel():
    try:
        import urllib.request
        with urllib.request.urlopen('http://127.0.0.1:4040/api/tunnels', timeout=2) as r:
            tunnels = json.loads(r.read())['tunnels']
            for t in tunnels:
                if t['public_url'].startswith('https://'):
                    return t['public_url']
            return tunnels[0]['public_url']
    except:
        return 'http://localhost:5050'

# ── HTTP Routes ──────────────────────────────────────────────────────────────

async def handle_incoming(request):
    """Generate TwiML that connects the call to a bidirectional Media Stream."""
    data = await request.post()
    call_sid = data.get('CallSid', 'unknown')
    caller = data.get('From', 'unknown')
    purpose = request.query.get('purpose', 'Check in and be helpful.')
    callee = request.query.get('callee', '')

    sessions[call_sid] = {
        'purpose': purpose,
        'callee': callee,
        'caller': caller,
        'profile': VOICE_PROFILES.get(caller, {}),
        'notes': [],
        'transcript': [],
        'start_time': time.time()
    }

    log.info(f"Call {call_sid[:8]} | to={callee or '?'} | mission={purpose[:50]}")

    tunnel = get_tunnel()
    ws_url = tunnel.replace('https://', 'wss://').replace('http://', 'ws://') + '/media-stream'

    resp = VoiceResponse()
    connect = Connect()
    stream = Stream(url=ws_url)
    stream.parameter(name='callSid', value=call_sid)
    stream.parameter(name='purpose', value=purpose)
    stream.parameter(name='callee', value=callee)
    connect.append(stream)
    resp.append(connect)

    return web.Response(text=str(resp), content_type='text/xml')


async def handle_status(request):
    """Handle Twilio call status webhooks."""
    data = await request.post()
    status = data.get('CallStatus', '')
    sid = data.get('CallSid', '')
    log.info(f"Status: {status} ({sid[:8] if sid else '?'})")
    if status == 'completed' and sid in sessions:
        session = sessions.pop(sid)
        asyncio.create_task(process_call_end(sid, session))
    return web.Response(text='OK')

# ── WebSocket Bridge: Twilio <-> OpenAI Realtime ─────────────────────────────

async def handle_media_stream(request):
    """Bridge bidirectional audio between Twilio Media Stream and OpenAI Realtime API."""
    ws_twilio = web.WebSocketResponse()
    await ws_twilio.prepare(request)
    log.info("Media stream connected")

    # Wait for Twilio's start event to get call context
    call_sid = stream_sid = purpose = callee = None
    async for msg in ws_twilio:
        if msg.type == aiohttp.WSMsgType.TEXT:
            data = json.loads(msg.data)
            if data.get('event') == 'start':
                stream_sid = data['start']['streamSid']
                call_sid = data['start']['callSid']
                cp = data['start'].get('customParameters', {})
                purpose = cp.get('purpose', '')
                callee = cp.get('callee', '')
                break
        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
            return ws_twilio

    if not stream_sid:
        return ws_twilio

    # Fall back to session state if custom params missing
    session = sessions.get(call_sid, {})
    purpose = purpose or session.get('purpose', 'Be helpful.')
    callee = callee or session.get('callee', '')

    log.info(f"Stream {stream_sid[:8]} | call {call_sid[:8]} | {callee or 'unknown'}")

    # Connect to OpenAI Realtime API and bridge audio
    try:
        async with aiohttp.ClientSession() as http:
            async with http.ws_connect(
                f"wss://api.openai.com/v1/realtime?model={REALTIME_MODEL}",
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "OpenAI-Beta": "realtime=v1"
                },
                heartbeat=30
            ) as ws_openai:
                # Configure the realtime session
                await ws_openai.send_json({
                    "type": "session.update",
                    "session": {
                        "modalities": ["text", "audio"],
                        "instructions": build_system_prompt(purpose, callee),
                        "voice": VOICE,
                        "input_audio_format": "g711_ulaw",
                        "output_audio_format": "g711_ulaw",
                        "input_audio_transcription": {"model": "whisper-1"},
                        "turn_detection": {
                            "type": "server_vad",
                            "threshold": 0.5,
                            "prefix_padding_ms": 300,
                            "silence_duration_ms": 500
                        },
                        "tools": get_tools(),
                        "tool_choice": "auto",
                        "temperature": 0.7
                    }
                })

                # Trigger the opening greeting
                greeting_ctx = (
                    f"The call just connected. {'Address ' + callee + ' by name.' if callee else 'Find out who you are speaking with.'} "
                    f"Begin your mission: {purpose}"
                )
                await ws_openai.send_json({
                    "type": "conversation.item.create",
                    "item": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": greeting_ctx}]
                    }
                })
                await ws_openai.send_json({"type": "response.create"})

                log.info(f"Realtime ready | voice={VOICE} | model={REALTIME_MODEL}")

                # Run both relay directions concurrently
                task_t2o = asyncio.create_task(
                    relay_twilio_to_openai(ws_twilio, ws_openai, call_sid)
                )
                task_o2t = asyncio.create_task(
                    relay_openai_to_twilio(ws_openai, ws_twilio, stream_sid, call_sid)
                )

                done, pending = await asyncio.wait(
                    [task_t2o, task_o2t], return_when=asyncio.FIRST_COMPLETED
                )
                for t in pending:
                    t.cancel()
                    try:
                        await t
                    except asyncio.CancelledError:
                        pass

    except Exception as e:
        log.error(f"Bridge error: {e}")

    if not ws_twilio.closed:
        await ws_twilio.close()
    return ws_twilio


async def relay_twilio_to_openai(ws_twilio, ws_openai, call_sid):
    """Forward caller audio from Twilio to OpenAI Realtime."""
    try:
        async for msg in ws_twilio:
            if msg.type == aiohttp.WSMsgType.TEXT:
                data = json.loads(msg.data)
                event = data.get('event')
                if event == 'media':
                    await ws_openai.send_json({
                        "type": "input_audio_buffer.append",
                        "audio": data['media']['payload']
                    })
                elif event == 'stop':
                    log.info("Twilio stream stopped")
                    return
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                return
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.error(f"Twilio->OpenAI relay error: {e}")


async def relay_openai_to_twilio(ws_openai, ws_twilio, stream_sid, call_sid):
    """Forward AI audio from OpenAI Realtime to Twilio. Handle tool calls and transcription."""
    try:
        async for msg in ws_openai:
            if msg.type == aiohttp.WSMsgType.TEXT:
                data = json.loads(msg.data)
                evt = data.get('type', '')

                # Audio chunk -> stream to Twilio
                if evt == 'response.audio.delta':
                    if not ws_twilio.closed:
                        await ws_twilio.send_json({
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {"payload": data['delta']}
                        })

                # Caller started speaking -> clear Twilio playback buffer (interruption)
                elif evt == 'input_audio_buffer.speech_started':
                    if not ws_twilio.closed:
                        await ws_twilio.send_json({
                            "event": "clear",
                            "streamSid": stream_sid
                        })
                    log.info(">> Interrupted")

                # Roberto's speech transcribed
                elif evt == 'response.audio_transcript.done':
                    t = data.get('transcript', '')
                    if t:
                        log.info(f"Roberto: {t[:100]}")
                        s = sessions.get(call_sid)
                        if s:
                            s.setdefault('transcript', []).append(f"Roberto: {t}")

                # Caller's speech transcribed
                elif evt == 'conversation.item.input_audio_transcription.completed':
                    t = data.get('transcript', '')
                    if t:
                        log.info(f"Caller: {t[:100]}")
                        s = sessions.get(call_sid)
                        if s:
                            s.setdefault('transcript', []).append(f"Caller: {t}")

                # Function call completed
                elif evt == 'response.function_call_arguments.done':
                    await handle_tool_call(data, ws_openai, call_sid)

                # Session events
                elif evt == 'session.created':
                    log.info("OpenAI Realtime session created")
                elif evt == 'session.updated':
                    log.info("Session configured")

                # Errors
                elif evt == 'error':
                    err = data.get('error', {})
                    log.error(f"Realtime error: {err.get('message', err)}")

            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                return

    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.error(f"OpenAI->Twilio relay error: {e}")


async def handle_tool_call(data, ws_openai, call_sid):
    """Process function calls from the Realtime API (agent actions)."""
    fn_name = data.get('name', '')
    call_id = data.get('call_id', '')
    try:
        args = json.loads(data.get('arguments', '{}'))
    except json.JSONDecodeError:
        args = {}

    result = ""

    if fn_name == 'take_note':
        note = args.get('note', '')
        category = args.get('category', 'detail')
        s = sessions.get(call_sid)
        if s:
            s.setdefault('notes', []).append({
                'category': category,
                'note': note,
                'time': time.time()
            })
        log.info(f"[note/{category}] {note}")
        result = "Noted."

    elif fn_name == 'end_call':
        outcome = args.get('outcome', 'Call ended')
        success = args.get('success', False)
        s = sessions.get(call_sid)
        if s:
            s['outcome'] = outcome
            s['success'] = success
        icon = 'DONE' if success else 'INCOMPLETE'
        log.info(f"[{icon}] {outcome}")
        result = "Ending call now."
        asyncio.create_task(delayed_hangup(call_sid))

    # Send function result back so the model can continue
    await ws_openai.send_json({
        "type": "conversation.item.create",
        "item": {
            "type": "function_call_output",
            "call_id": call_id,
            "output": result
        }
    })
    await ws_openai.send_json({"type": "response.create"})


async def delayed_hangup(call_sid, delay=3):
    """Hang up after a short delay so the goodbye audio can finish playing."""
    await asyncio.sleep(delay)
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, lambda: twilio_client.calls(call_sid).update(status='completed')
        )
        log.info(f"Hung up {call_sid[:8]}")
    except Exception as e:
        log.error(f"Hangup error: {e}")

# ── Post-Call Processing ─────────────────────────────────────────────────────

async def process_call_end(call_sid, session):
    """Download recording, transcribe with Whisper, summarize, and save."""
    await asyncio.sleep(6)  # Wait for Twilio to finalize recording
    try:
        import openai
        oai = openai.OpenAI(api_key=OPENAI_API_KEY)

        transcript_text = "\n".join(session.get('transcript', []))
        notes = session.get('notes', [])
        purpose = session.get('purpose', '')
        callee = session.get('callee', 'unknown')
        outcome = session.get('outcome', '')
        success = session.get('success')
        dur = int(time.time() - session.get('start_time', time.time()))
        date_str = datetime.datetime.now().strftime('%Y-%m-%d')

        # Try to download Twilio recording for higher-quality transcription
        wav_bytes = None
        try:
            loop = asyncio.get_event_loop()
            recs = await loop.run_in_executor(
                None, lambda: twilio_client.recordings.list(call_sid=call_sid, limit=1)
            )
            if recs:
                import urllib.request
                rec = recs[0]
                url = f"https://api.twilio.com{rec.uri.replace('.json', '.wav')}"
                pm = urllib.request.HTTPPasswordMgrWithDefaultRealm()
                pm.add_password(None, url, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
                opener = urllib.request.build_opener(urllib.request.HTTPBasicAuthHandler(pm))
                wav_bytes = await loop.run_in_executor(None, lambda: opener.open(url).read())
                log.info(f"Recording downloaded: {len(wav_bytes) // 1024}KB")
        except Exception as e:
            log.info(f"No Twilio recording available: {e}")

        # Whisper transcription from recording (better than realtime transcription)
        if wav_bytes:
            try:
                import io
                f = io.BytesIO(wav_bytes)
                f.name = "recording.wav"
                result = oai.audio.transcriptions.create(
                    model='gpt-4o-transcribe', file=f, response_format='text'
                )
                if result and result.strip():
                    transcript_text = result
            except Exception:
                pass

        # Summarize the call
        notes_text = "\n".join([f"- [{n['category']}] {n['note']}" for n in notes]) if notes else "None"

        resp = oai.chat.completions.create(
            model='gpt-4o',
            messages=[{"role": "user", "content": f"""Phone call summary request.
Date: {date_str}. Called: {callee}. Purpose: {purpose}. Duration: {dur}s.
Agent outcome: {outcome}. Mission success: {success}.
Notes taken during call:
{notes_text}
Transcript:
{transcript_text[:2000]}

Return JSON only:
{{
  "folder_name": "{date_str}-<topic-slug-3-5-words-hyphens>",
  "summary": "3-5 bullet points: who was called, what was discussed, outcomes, follow-ups"
}}"""}],
            response_format={"type": "json_object"}
        ).choices[0].message.content

        parsed = json.loads(resp)
        folder = parsed.get('folder_name', f'{date_str}-call').replace(' ', '-')[:60]
        summary = parsed.get('summary', 'No summary')

        call_dir = os.path.join(RECORDINGS_DIR, folder)
        os.makedirs(call_dir, exist_ok=True)
        if wav_bytes:
            with open(os.path.join(call_dir, 'recording.wav'), 'wb') as f:
                f.write(wav_bytes)
        with open(os.path.join(call_dir, 'transcript.txt'), 'w') as f:
            f.write(transcript_text)
        with open(os.path.join(call_dir, 'summary.txt'), 'w') as f:
            f.write(summary if isinstance(summary, str) else '\n'.join(summary))
        with open(os.path.join(call_dir, 'meta.json'), 'w') as f:
            json.dump({
                'call_sid': call_sid, 'callee': callee, 'purpose': purpose,
                'duration_sec': dur, 'date': date_str, 'outcome': outcome,
                'success': success, 'notes': notes
            }, f, indent=2)

        log.info(f"Saved: recordings/{folder}/")
        log.info(f"Summary: {summary[:200] if isinstance(summary, str) else summary}")

    except Exception as e:
        log.error(f"Post-call processing error: {e}")

# ── App ──────────────────────────────────────────────────────────────────────

app = web.Application()
app.router.add_post('/voice/incoming', handle_incoming)
app.router.add_post('/voice/status', handle_status)
app.router.add_get('/media-stream', handle_media_stream)

if __name__ == '__main__':
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5050
    log.info(f"Roberto v5 | OpenAI Realtime | port {port}")
    log.info(f"Voice: {VOICE} | Model: {REALTIME_MODEL}")
    log.info(f"Target latency: <300ms (audio-to-audio, zero STT/TTS pipeline)")
    web.run_app(app, host='0.0.0.0', port=port, print=None)
