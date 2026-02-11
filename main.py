import os
import json
import base64
import asyncio
from datetime import datetime

import websockets
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import JSONResponse, Response
from fastapi.websockets import WebSocketDisconnect
from twilio.twiml.voice_response import VoiceResponse, Connect

load_dotenv()

# ====== ENV ======
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL")  # e.g. https://speech-assistant-...onrender.com
PORT = int(os.getenv("PORT", "10000"))          # Render sets $PORT; default 10000 is safe on Render
TEMPERATURE = float(os.getenv("TEMPERATURE", "0.8"))

# Realtime session config
VOICE = os.getenv("VOICE", "alloy")
SYSTEM_MESSAGE = os.getenv(
    "SYSTEM_MESSAGE",
    "You are a helpful voice assistant. Keep responses concise and useful."
)

LOG_EVENT_TYPES = {
    "error",
    "session.created",
    "session.updated",
    "response.done",
    "rate_limits.updated",
    "input_audio_buffer.speech_started",
    "input_audio_buffer.speech_stopped",
}
SHOW_TIMING_MATH = False

if not OPENAI_API_KEY:
    raise ValueError("Missing OPENAI_API_KEY")

app = FastAPI()

# ====== HELPERS ======
def _public_host_from_env_or_request(request: Request) -> str:
    """
    Twilio needs a public WSS URL. Best practice: set PUBLIC_BASE_URL and derive host from it.
    Fallback: request.url.hostname (may be wrong behind some proxies).
    """
    if PUBLIC_BASE_URL:
        # Strip scheme and path, keep host
        # e.g. https://x.onrender.com -> x.onrender.com
        host = PUBLIC_BASE_URL.replace("https://", "").replace("http://", "").split("/")[0]
        return host
    return request.url.hostname

def build_twiml_bridge(request: Request) -> str:
    host = _public_host_from_env_or_request(request)
    vr = VoiceResponse()
    # Minimal pre-bridge prompt (optional). Keep it short.
    # Remove entirely if you want zero TTS before bridge.
    # vr.say("Connecting.", voice="alice")
    connect = Connect()
    connect.stream(url=f"wss://{host}/media-stream")
    vr.append(connect)
    return str(vr)

async def initialize_session(openai_ws):
    session_update = {
        "type": "session.update",
        "session": {
            "type": "realtime",
            "model": "gpt-realtime",
            "instructions": SYSTEM_MESSAGE,
            "output_modalities": ["audio"],
            "audio": {
                "input": {
                    "format": {"type": "audio/pcmu"},
                    "turn_detection": {"type": "server_vad"},
                },
                "output": {
                    "format": {"type": "audio/pcmu"},
                    "voice": VOICE,
                },
            },
            "temperature": TEMPERATURE,
        },
    }
    await openai_ws.send(json.dumps(session_update))

# ====== ROUTES ======
@app.get("/", response_class=JSONResponse)
async def health():
    return {
        "ok": True,
        "ts": datetime.utcnow().isoformat() + "Z",
        "service": "twilio-openai-realtime",
    }

@app.api_route("/incoming-call", methods=["GET", "POST"])
async def incoming_call(request: Request):
    twiml = build_twiml_bridge(request)
    return Response(twiml, media_type="application/xml")

@app.api_route("/outbound-call", methods=["GET", "POST"])
async def outbound_call(request: Request):
    # SAME behavior as incoming: immediate bridge
    twiml = build_twiml_bridge(request)
    return Response(twiml, media_type="application/xml")

@app.websocket("/media-stream")
async def media_stream(websocket: WebSocket):
    await websocket.accept()

    # Connect to OpenAI Realtime
    async with websockets.connect(
        f"wss://api.openai.com/v1/realtime?model=gpt-realtime",
        extra_headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "OpenAI-Beta": "realtime=v1",
        },
    ) as openai_ws:
        await initialize_session(openai_ws)

        stream_sid = None
        latest_media_timestamp = 0
        last_assistant_item = None
        mark_queue = []
        response_start_timestamp_twilio = None

        async def send_mark():
            nonlocal stream_sid
            if stream_sid:
                mark_event = {
                    "event": "mark",
                    "streamSid": stream_sid,
                    "mark": {"name": "responsePart"},
                }
                await websocket.send_json(mark_event)
                mark_queue.append("responsePart")

        async def receive_from_twilio():
            nonlocal stream_sid, latest_media_timestamp, response_start_timestamp_twilio, last_assistant_item
            try:
                async for msg in websocket.iter_text():
                    data = json.loads(msg)

                    event = data.get("event")
                    if event == "start":
                        stream_sid = data["start"]["streamSid"]
                        latest_media_timestamp = 0
                        response_start_timestamp_twilio = None
                        last_assistant_item = None
                        mark_queue.clear()

                    elif event == "media":
                        latest_media_timestamp = int(data["media"]["timestamp"])
                        payload_b64 = data["media"]["payload"]

                        # Forward to OpenAI input buffer
                        await openai_ws.send(json.dumps({
                            "type": "input_audio_buffer.append",
                            "audio": payload_b64,
                        }))

                    elif event == "mark":
                        if mark_queue:
                            mark_queue.pop(0)

                    elif event == "stop":
                        break

            except WebSocketDisconnect:
                if openai_ws.open:
                    await openai_ws.close()

        async def send_to_twilio():
            nonlocal response_start_timestamp_twilio, last_assistant_item
            try:
                async for msg in openai_ws:
                    resp = json.loads(msg)
                    rtype = resp.get("type")

                    if rtype in LOG_EVENT_TYPES:
                        print("OpenAI event:", rtype)

                    # Audio back to Twilio
                    if rtype == "response.output_audio.delta" and resp.get("delta"):
                        audio_payload = base64.b64encode(
                            base64.b64decode(resp["delta"])
                        ).decode("utf-8")

                        await websocket.send_json({
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {"payload": audio_payload},
                        })

                        # Track item boundaries for interruption/truncation
                        if resp.get("item_id") and resp["item_id"] != last_assistant_item:
                            response_start_timestamp_twilio = latest_media_timestamp
                            last_assistant_item = resp["item_id"]
                            if SHOW_TIMING_MATH:
                                print("New item start @", response_start_timestamp_twilio)

                        await send_mark()

                    # Optional: interruption when caller starts speaking
                    if rtype == "input_audio_buffer.speech_started" and last_assistant_item:
                        if mark_queue and response_start_timestamp_twilio is not None:
                            elapsed = latest_media_timestamp - response_start_timestamp_twilio

                            await openai_ws.send(json.dumps({
                                "type": "conversation.item.truncate",
                                "item_id": last_assistant_item,
                                "content_index": 0,
                                "audio_end_ms": max(0, elapsed),
                            }))

                            await websocket.send_json({"event": "clear", "streamSid": stream_sid})
                            mark_queue.clear()
                            last_assistant_item = None
                            response_start_timestamp_twilio = None

            except Exception as e:
                print("send_to_twilio error:", repr(e))

        await asyncio.gather(receive_from_twilio(), send_to_twilio())

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
