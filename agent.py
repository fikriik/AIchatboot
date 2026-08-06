import os
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types

from tools import TOOL_DECLARATIONS, TOOL_FUNCTIONS

load_dotenv()


# config

MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
THINKING_LEVEL_STR = os.getenv("GEMINI_THINKING_LEVEL", "minimal").lower()
MAX_TOOL_ITERATIONS = 5

AI_NAME = os.getenv("AI_NAME", "Gita")
COMPANY_NAME = os.getenv("COMPANY_NAME", "Glowria Aesthetic Clinic")

FALLBACK_MESSAGE = ("Mohon maaf kak, whatsapp kami sedang ada sedikit kendala🙏[NEXT]"
                    "Admin kami akan segera membantu ya kak 😊[HANDOVER]")

_client: genai.Client | None = None

def get_client() -> genai.Client:
    """Lazy init: client baru dibuat saat pertama dibutuhkan."""
    global _client
    if _client is None:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY belum diisi. Salin .env.example ke .env "
                            "dan isi API key dari https://aistudio.google.com")
        _client = genai.Client(api_key=api_key)
    return _client

HARI = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]
BULAN = ["Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli",
        "Agustus", "September", "Oktober", "November", "Desember"]


def render_system_prompt() -> str:
    """Baca prompts/system.md dan isi template variables."""
    prompt_file = Path(__file__).parent / "prompts" / "system.md"
    if not prompt_file.exists():
        # Fallback jika file system.md belum dibuat
        return f"Kamu adalah {AI_NAME}, customer service dari {COMPANY_NAME}."

    raw = prompt_file.read_text(encoding="utf-8")

    replacements = {
        "{{aiName}}": AI_NAME,
        "{{companyName}}": COMPANY_NAME,
        "{{currentDateContext}}": (
            "Tanggal dan jam SAAT INI selalu diberikan di awal setiap pesan customer "
            "(dalam tanda [Konteks: ...]). Jadikan itu satu-satunya acuan waktu untuk "
            "menghitung 'hari ini', 'besok', 'lusa', dll. JANGAN PERNAH menawarkan atau "
            "menyetujui booking untuk jam yang sudah lewat."
        ),
        "{{bookingContext}}": ("<booking_context>Gunakan tool check_available_schedule dan "
                            "book_treatment untuk status slot real-time. Jangan pernah "
                            "berasumsi soal ketersediaan tanpa hasil tool.</booking_context>"),
        "{{leadContext}}": "Lihat info customer di awal percakapan.",
        "{{conversationSummary}}": "Lihat history percakapan di contents.",
        "{{knowledgeBase}}": ("Gunakan tool search_treatments untuk data treatment dan harga. "
                            "Info klinik statis ada di <clinic_info>."),
        "{{additionalContext}}": "",
    }
    for key, value in replacements.items():
        raw = raw.replace(key, value)
    return raw

SYSTEM_PROMPT = render_system_prompt()

# Konversi THINKING_LEVEL_STR ke thinking_budget (token integer)
if THINKING_LEVEL_STR in {"minimal", "low"}:
    thinking_budget = 1024
elif THINKING_LEVEL_STR in {"none", "off"}:
    thinking_budget = 0
else:
    thinking_budget = 2048

# Buat config yang aman untuk SDK google-genai
GENERATE_CONFIG = types.GenerateContentConfig(
    system_instruction=SYSTEM_PROMPT,
    tools=[types.Tool(function_declarations=TOOL_DECLARATIONS)],
    thinking_config=types.ThinkingConfig(thinking_budget=thinking_budget) if thinking_budget > 0 else None,
    temperature=0.7,
)

# Agent Core Loop

def run_agent(history: list, user_message: str, phone: str,
            images: list[tuple[bytes, str]] | None = None) -> tuple[str, list]:
    """Jalankan satu giliran percakapan."""
    now = datetime.now()
    date_ctx = (f"Hari ini {HARI[now.weekday()]}, {now.day} {BULAN[now.month - 1]} {now.year} "
                f"({now:%Y-%m-%d}), pukul {now:%H:%M}")

    if not user_message:
        user_message = "(customer mengirim gambar tanpa teks)"

    parts = [types.Part(text=f"[Konteks: {date_ctx}. Nomor WhatsApp customer: {phone}]\n{user_message}")]
    for img_bytes, mime in (images or []):
        parts.append(types.Part.from_bytes(data=img_bytes, mime_type=mime))

    contents = list(history)
    contents.append(types.Content(role="user", parts=parts))

    try:
        for _ in range(MAX_TOOL_ITERATIONS):
            response = get_client().models.generate_content(
                model=MODEL,
                contents=contents,
                config=GENERATE_CONFIG,
            )

            candidate = response.candidates[0]
            contents.append(candidate.content)

            function_calls = response.function_calls or []
            if not function_calls:
                return (response.text or FALLBACK_MESSAGE), contents

            result_parts = []
            for fc in function_calls:
                print(f"  [tool] {fc.name}({dict(fc.args)})")
                func = TOOL_FUNCTIONS.get(fc.name)
                if func is None:
                    result = {"error": f"Tool '{fc.name}' tidak dikenal."}
                else:
                    try:
                        result = func(**dict(fc.args))
                    except Exception as e:
                        result = {"error": f"Tool gagal: {e}"}
                result_parts.append(types.Part.from_function_response(
                    name=fc.name, response={"result": result},
                ))
            contents.append(types.Content(role="user", parts=result_parts))

        return FALLBACK_MESSAGE, contents

    except Exception as e:
        print(f"  [error] Gemini API: {e}")
        return FALLBACK_MESSAGE, contents