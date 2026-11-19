"""Voice commands: Whisper speech-to-text (faster-whisper, baked into the image, CPU int8)
plus an LLM intent parser that recognises approve/reject decisions for pending trades."""
from __future__ import annotations

import asyncio
import logging
import os
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, Field

from app.config import get_settings

log = logging.getLogger(__name__)


@lru_cache
def _whisper():
    from faster_whisper import WhisperModel

    return WhisperModel(get_settings().whisper_model, device="cpu", compute_type="int8",
                        download_root=os.environ.get("WHISPER_CACHE", None))


def decode_audio(audio: bytes) -> "np.ndarray":
    """Any browser audio (webm/ogg/mp4/wav) -> 16 kHz mono float32, using the ffmpeg binary in the image.
    (Avoids faster-whisper's PyAV decoder, whose options broke with newer PyAV releases.)"""
    import subprocess

    import numpy as np

    proc = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
         "-f", "s16le", "-acodec", "pcm_s16le", "-ac", "1", "-ar", "16000", "pipe:1"],
        input=audio, capture_output=True, timeout=60, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg could not decode the audio: {proc.stderr.decode(errors='ignore')[:200]}")
    return np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0


async def transcribe(audio: bytes, suffix: str = ".webm") -> str:
    def _run():
        samples = decode_audio(audio)
        if samples.size < 1600:  # under 0.1 s of audio
            return ""
        segments, _ = _whisper().transcribe(
            samples, language=get_settings().whisper_language, vad_filter=True, beam_size=1,
            initial_prompt="Stock trading commands for NSE and MCX: Reliance, TCS, Infosys, HDFC Bank, "
                           "Nifty, Sensex, crude oil, gold, approve, reject, buy, sell, intraday, delivery.")
        return " ".join(seg.text.strip() for seg in segments).strip()

    return await asyncio.to_thread(_run)


class VoiceIntent(BaseModel):
    intent: Literal["approve", "reject", "command"] = Field(
        description="approve/reject only if the user is deciding on the pending trade; otherwise command")
    reason: str = Field(default="", description="the user's stated reason, if any")


async def parse_intent(transcript: str, has_pending: bool) -> VoiceIntent:
    if not has_pending:
        return VoiceIntent(intent="command")
    from app.agents.models import make_model

    try:
        model = make_model(get_settings().utility_model).with_structured_output(VoiceIntent)
        return await model.ainvoke(
            "A trader has a pending paper-trade awaiting their approval. Classify what they just said.\n"
            f'Said: "{transcript}"')
    except Exception as exc:  # deterministic fallback if the LLM is rate-limited
        log.warning("intent LLM failed (%s); keyword fallback", exc)
        t = transcript.lower()
        if any(w in t for w in ("approve", "go ahead", "confirm", "execute it", "yes do it")):
            return VoiceIntent(intent="approve")
        if any(w in t for w in ("reject", "cancel", "don't", "do not", "deny")):
            return VoiceIntent(intent="reject", reason=transcript)
        return VoiceIntent(intent="command")
