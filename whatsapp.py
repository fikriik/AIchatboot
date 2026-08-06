import asyncio
import base64
import os
import re

import httpx
from dotenv import load_dotenv

load_dotenv()

EVOLUTION_API_URL = os.getenv("EVOLUTION_API_URL", "http://localhost:8080").rstrip("/")
EVOLUTION_API_KEY = os.getenv("EVOLUTION_API_KEY", "")
EVOLUTION_INSTANCE = os.getenv("EVOLUTION_INSTANCE", "fikrii")

HEADERS = {"apikey": EVOLUTION_API_KEY, "Content-Type": "application/json"}

TYPING_SECONDS_PER_CHAR = 0.03   # ~33 karakter/detik
TYPING_MIN_SECONDS = 1.0
TYPING_MAX_SECONDS = 5.0


def _typing_duration(text: str) -> float:
    return min(max(len(text) * TYPING_SECONDS_PER_CHAR, TYPING_MIN_SECONDS), TYPING_MAX_SECONDS)


async def send_typing(client: httpx.AsyncClient, phone: str, seconds: float) -> None:
    """Tampilkan 'sedang mengetik...' di sisi customer."""
    try:
        await client.post(
            f"{EVOLUTION_API_URL}/chat/sendPresence/{EVOLUTION_INSTANCE}",
            headers=HEADERS,
            json={"number": phone, "presence": "composing",
                  "delay": int(seconds * 1000)},
        )
    except httpx.HTTPError:
        pass  # typing gagal bukan alasan membatalkan pengiriman pesan


async def send_text(client: httpx.AsyncClient, phone: str, text: str) -> bool:
    """Kirim satu bubble teks."""
    try:
        resp = await client.post(
            f"{EVOLUTION_API_URL}/message/sendText/{EVOLUTION_INSTANCE}",
            headers=HEADERS,
            json={"number": phone, "text": text},
        )
        resp.raise_for_status()
        return True
    except httpx.HTTPError as e:
        print(f"  [whatsapp] Gagal kirim ke {phone}: {e}")
        return False



async def send_reply(phone: str, reply: str) -> None:
    """Kirim jawaban AI lengkap: bersihkan marker, pecah bubble, kirim manusiawi.

    - [WAIT] dan [HANDOVER] adalah marker internal -> dibuang dari teks
    (deteksi [HANDOVER] dilakukan di main.py SEBELUM fungsi ini dipanggil)
    - [NEXT] memecah jadi bubble terpisah, dikirim berurutan dengan
    typing indicator + jeda proporsional
    """
    clean = reply.replace("[WAIT]", "").replace("[HANDOVER]", "")
    # Pecah bubble di [NEXT] ATAU baris kosong (\n\n) -> tiap paragraf jadi bubble sendiri
    bubbles = [b.strip() for b in re.split(r"\[NEXT\]|\n\s*\n", clean) if b.strip()]

    async with httpx.AsyncClient(timeout=30) as client:
        for bubble in bubbles:
            duration = _typing_duration(bubble)
            await send_typing(client, phone, duration)
            await asyncio.sleep(duration)
            await send_text(client, phone, bubble)



def extract_incoming(payload: dict) -> dict | None:
    """Parse webhook 'messages.upsert' Evolution API.

    Return dict berisi:
      phone, name, text, media_type ("image"|None), mime, message (raw data)

    Return None untuk event yang harus DIABAIKAN:
    - pesan dari diri sendiri (fromMe) -> mencegah AI membalas dirinya = loop
    - pesan grup (@g.us) -> agent ini untuk chat personal
    - tipe lain (voice/dokumen/stiker) -> di luar scope workshop
    """
    data = payload.get("data") or {}
    key = data.get("key") or {}
    remote_jid: str = key.get("remoteJid", "")

    if key.get("fromMe"):
        return None
    if "@g.us" in remote_jid:           # grup
        return None
    if not remote_jid.endswith("@s.whatsapp.net"):
        return None

    message = data.get("message") or {}
    base = {"phone": remote_jid.split("@")[0], "name": data.get("pushName")}

    # GAMBAR: caption (kalau ada) jadi teksnya, byte-nya diambil belakangan.
    image_msg = message.get("imageMessage")
    if image_msg is not None:
        return {**base,
                "text": image_msg.get("caption") or "",
                "media_type": "image",
                "mime": image_msg.get("mimetype", "image/jpeg"),
                "message": data}

    # TEKS biasa
    text = (message.get("conversation")
            or (message.get("extendedTextMessage") or {}).get("text"))
    if not text:
        return None                      # tipe lain yang belum didukung

    return {**base, "text": text, "media_type": None, "mime": None, "message": data}


async def fetch_media_b64(client: httpx.AsyncClient, message: dict) -> tuple[bytes, str] | None:
    """Minta Evolution men-decrypt media WhatsApp -> (bytes, mime).

    WhatsApp mengirim media terenkripsi; byte mentahnya TIDAK ada di webhook.
    Endpoint ini yang mengembalikan base64 siap pakai.
    """
    try:
        resp = await client.post(
            f"{EVOLUTION_API_URL}/chat/getBase64FromMediaMessage/{EVOLUTION_INSTANCE}",
            headers=HEADERS,
            json={"message": message, "convertToMp4": False},
        )
        resp.raise_for_status()
        body = resp.json()
        b64 = body.get("base64")
        mime = body.get("mimetype") or "image/jpeg"
        if b64:
            return base64.b64decode(b64), mime
        print(f"  [media] respons tanpa base64, keys={list(body.keys())}")
    except httpx.HTTPError as e:
        print(f"  [media] gagal ambil base64: {e}")
    return None