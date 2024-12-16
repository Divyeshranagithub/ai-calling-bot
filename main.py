import asyncio
import base64
import json
import logging
import sys
import traceback
import websockets
import ssl
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.responses import HTMLResponse
from typing import Dict, Any, Optional
from twilio.twiml.voice_response import VoiceResponse, Connect

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('app.log')
    ]
)
logger = logging.getLogger(__name__)

app = FastAPI()

class DeepgramSTSHandler:
    def __init__(self, deepgram_api_key: str):
        self.deepgram_api_key = deepgram_api_key
        self.audio_queue = asyncio.Queue()
        self.streamsid_queue = asyncio.Queue()
        self.is_connection_active = False

    async def connect_sts(self) -> websockets.WebSocketClientProtocol:
        """Establish connection to Deepgram STS WebSocket with enhanced error handling"""
        try:
            # Convert headers to list of tuples for compatibility
            headers = [("Authorization", f"Token {self.deepgram_api_key}")]

            # Add SSL context for secure connection
            ssl_context = ssl.create_default_context()

            # Set a connection timeout
            connection_timeout = 10  # seconds

            logger.info("Attempting to connect to Deepgram STS WebSocket")

            sts_ws = await asyncio.wait_for(
                websockets.connect(
                    "wss://sts.sandbox.deepgram.com/agent",
                    header=headers,
                    ssl=ssl_context
                ),
                timeout=connection_timeout
            )

            logger.info("Successfully connected to Deepgram STS WebSocket")
            self.is_connection_active = True
            return sts_ws

        except asyncio.TimeoutError:
            logger.error("Connection to Deepgram STS timed out")
            raise
        except websockets.exceptions.WebSocketException as e:
            logger.error(f"WebSocket connection error: {e}")
            raise
        except Exception as e:
            logger.error(f"Unexpected error connecting to Deepgram STS: {e}")
            logger.error(traceback.format_exc())
            raise

    async def send_sts_configuration(self, sts_ws: websockets.WebSocketClientProtocol):
        """Send initial configuration to STS with error handling"""
        try:
            config_message = {
                "type": "SettingsConfiguration",
                "audio": {
                    "input": {
                        "encoding": "mulaw",
                        "sample_rate": 8000,
                    },
                    "output": {
                        "encoding": "mulaw",
                        "sample_rate": 8000,
                        "container": "none",
                    },
                },
                "agent": {
                    "listen": {"model": "nova-2"},
                    "think": {
                        "provider": {
                            "type": "anthropic",
                        },
                        "model": "claude-3-haiku-20240307",
                        "instructions": "You are a helpful car seller.",
                    },
                    "speak": {"model": "aura-asteria-en"},
                },
            }
            await sts_ws.send(json.dumps(config_message))
            logger.info("STS configuration sent successfully")
        except Exception as e:
            logger.error(f"Error sending STS configuration: {e}")
            raise

    async def sts_sender(self, sts_ws: websockets.WebSocketClientProtocol):
        """Send audio chunks from queue to STS with error handling"""
        try:
            while self.is_connection_active:
                try:
                    chunk = await asyncio.wait_for(self.audio_queue.get(), timeout=5.0)
                    await sts_ws.send(chunk)
                except asyncio.TimeoutError:
                    # No audio for 5 seconds, check if connection is still active
                    if not self.is_connection_active:
                        break
                    continue
        except Exception as e:
            logger.error(f"Error in STS sender: {e}")
            self.is_connection_active = False

    async def sts_receiver(self, sts_ws: websockets.WebSocketClientProtocol, twilio_ws: WebSocket):
        """Receive messages from STS and handle them with error handling"""
        try:
            streamsid = await self.streamsid_queue.get()

            async for message in sts_ws:
                try:
                    if isinstance(message, str):
                        # Handle text messages (like barge-in events)
                        decoded = json.loads(message)
                        if decoded['type'] == 'UserStartedSpeaking':
                            await twilio_ws.send_json({
                                "event": "clear",
                                "streamSid": streamsid
                            })
                        continue

                    # Send TTS audio back to Twilio
                    raw_mulaw = message
                    await twilio_ws.send_json({
                        "event": "media",
                        "streamSid": streamsid,
                        "media": {"payload": base64.b64encode(raw_mulaw).decode("ascii")}
                    })
                except Exception as inner_e:
                    logger.error(f"Error processing STS message: {inner_e}")
        except Exception as e:
            logger.error(f"Error in STS receiver: {e}")
            self.is_connection_active = False

    async def twilio_receiver(self, twilio_ws: WebSocket):
        """Receive and process Twilio WebSocket messages with error handling"""
        BUFFER_SIZE = 20 * 160
        inbuffer = bytearray(b"")
        logger.info("Starting Twilio WebSocket receiver")

        try:
            while True:
                try:
                    data = await twilio_ws.receive_json()
                    logger.debug(f"Received Twilio event: {data.get('event')}")

                    if data["event"] == "start":
                        streamsid = data["start"]["streamSid"]
                        await self.streamsid_queue.put(streamsid)
                        logger.info(f"Stream started with ID: {streamsid}")

                    if data["event"] == "media":
                        media = data["media"]
                        chunk = base64.b64decode(media["payload"])
                        if media["track"] == "inbound":
                            inbuffer.extend(chunk)

                    # Process buffer in chunks
                    while len(inbuffer) >= BUFFER_SIZE:
                        chunk = inbuffer[:BUFFER_SIZE]
                        await self.audio_queue.put(chunk)
                        inbuffer = inbuffer[BUFFER_SIZE:]

                    if data["event"] == "stop":
                        logger.info("Twilio stream stopped")
                        break

                except json.JSONDecodeError:
                    logger.warning("Received invalid JSON from Twilio")
                except Exception as inner_e:
                    logger.error(f"Error processing Twilio message: {inner_e}")
                    break

        except WebSocketDisconnect:
            logger.warning("Twilio WebSocket disconnected")
        except Exception as e:
            logger.error(f"Unexpected error in Twilio receiver: {e}")
        finally:
            self.is_connection_active = False

    async def handle_twilio_connection(self, twilio_ws: WebSocket):
        """Main handler for Twilio WebSocket connection with robust error handling"""
        try:
            await twilio_ws.accept()
            logger.info("Twilio WebSocket connection accepted")

            async with await self.connect_sts() as sts_ws:
                await self.send_sts_configuration(sts_ws)

                # Run concurrent tasks with error handling
                tasks = [
                    self.sts_sender(sts_ws),
                    self.sts_receiver(sts_ws, twilio_ws),
                    self.twilio_receiver(twilio_ws)
                ]

                try:
                    await asyncio.gather(*tasks)
                except Exception as gather_e:
                    logger.error(f"Error in concurrent tasks: {gather_e}")
                    raise

        except Exception as e:
            logger.error(f"Error in Twilio connection handler: {e}")
            logger.error(traceback.format_exc())
            # Attempt to close the WebSocket
            try:
                await twilio_ws.close(code=1011)  # Internal server error
            except Exception:
                pass
        finally:
            self.is_connection_active = False

@app.websocket("/media-stream")
async def twilio_websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for Twilio connections with enhanced error handling"""
    try:
        deepgram_api_key = "b749e403c7141d930ad7cd1e7b7f5d4a96e114b5"  # Replace with secure key management
        handler = DeepgramSTSHandler(deepgram_api_key)
        await handler.handle_twilio_connection(websocket)
    except Exception as e:
        logger.error(f"Unhandled error in WebSocket endpoint: {e}")
        logger.error(traceback.format_exc())
        # Attempt to close the WebSocket
        try:
            await websocket.close(code=1011)  # Internal server error
        except Exception:
            pass

@app.api_route("/incoming-call", methods=["GET", "POST"])
async def handle_incoming_call(request: Request):
    """Handle incoming call and return TwiML response to connect to Media Stream."""
    try:
        response = VoiceResponse()
        response.say(
            "Please wait while we connect your call to the A. I. voice assistant, powered by and.ai."
        )
        response.pause(length=1)
        response.say("O.K. you can start talking!")

        # Safely get the host
        try:
            host = request.url.hostname
            if not host:
                raise ValueError("Unable to determine host")
        except Exception as e:
            logger.error(f"Error determining host: {e}")

        connect = Connect()
        connect.stream(url=f'wss://{host}/media-stream')
        response.append(connect)

        return HTMLResponse(content=str(response), media_type="application/xml")

    except Exception as e:
        logger.error(f"Error in incoming call handler: {e}")
        raise HTTPException(status_code=500, detail="Internal Server Error")

# Configure global exception handler
@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    """Global exception handler for the entire application"""
    logger.error(f"Unhandled exception: {exc}")
    logger.error(traceback.format_exc())
    return HTMLResponse(status_code=500, content="Internal Server Error")

# Run with: uvicorn main:app --host 0.0.0.0 --port 8000
if __name__ == "__main__":
    import uvicorn

    # Configure uvicorn logging
    logging.getLogger("uvicorn").handlers.clear()
    uvicorn.run(app, host="0.0.0.0", port=8000)