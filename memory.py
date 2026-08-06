import os
from datetime import datetime, timedelta
from typing import Optional

from dotenv import load_dotenv
from google.genai import types
from sqlmodel import select

from database import Contact, ConversationMessage, get_session

load_dotenv()

MEMORY_WINDOW = int(os.getenv("MEMORY_WINDOW", "20"))      # jumlah pesan
SESSION_TTL_HOURS = int(os.getenv("SESSION_TTL_HOURS", "24"))  # 0 = nonaktif


class Memory:
    """Interface memory. Kalau suatu hari mau ganti ke Redis,
    cukup tulis ulang class ini, agent & webhook tidak berubah."""

    def get_history(self, phone: str) -> list:
        """Ambil history sebagai list types.Content, siap untuk run_agent.

        Sliding window: hanya MEMORY_WINDOW pesan terakhir.
        Session TTL: kalau pesan terakhir lebih tua dari TTL, mulai sesi baru
        (history kosong) supaya AI tidak merespons konteks basi kemarin-kemarin.
        """
        with get_session() as session:
            rows = session.exec(
                select(ConversationMessage)
                .where(ConversationMessage.phone == phone)
                .order_by(ConversationMessage.created_at.desc())
                .limit(MEMORY_WINDOW)
            ).all()

        if not rows:
            return []

        rows = list(reversed(rows))  # kembalikan ke urutan kronologis

        if SESSION_TTL_HOURS > 0:
            last = rows[-1].created_at
            if datetime.now() - last > timedelta(hours=SESSION_TTL_HOURS):
                return []  # sesi baru; history lama tetap ada di DB untuk dashboard

        return [
            types.Content(role=m.role, parts=[types.Part(text=m.text)])
            for m in rows
        ]

    def add(self, phone: str, role: str, text: str) -> None:
        """Simpan satu pesan. role: 'user' | 'model'."""
        with get_session() as session:
            session.add(ConversationMessage(phone=phone, role=role, text=text))
            session.commit()

    def is_ai_enabled(self, phone: str) -> bool:
        """Cek saklar takeover. False = admin sedang pegang percakapan ini."""
        with get_session() as session:
            contact = session.get(Contact, phone)
            return contact.ai_enabled if contact else True

    def set_ai_enabled(self, phone: str, enabled: bool) -> None:
        """Saklar takeover: dipakai saat [HANDOVER] terdeteksi & oleh dashboard."""
        with get_session() as session:
            contact = session.get(Contact, phone)
            if contact:
                contact.ai_enabled = enabled
            else:
                session.add(Contact(phone=phone, ai_enabled=enabled))
            session.commit()

    def set_name(self, phone: str, name: str) -> None:
        """Simpan nama WhatsApp (pushName) customer supaya tampil di dashboard.

        Dipanggil tiap pesan masuk. Membuat Contact kalau belum ada,
        atau memperbarui namanya kalau berubah.
        """
        if not name:
            return
        with get_session() as session:
            contact = session.get(Contact, phone)
            if contact:
                if contact.name != name:
                    contact.name = name
                    session.commit()
            else:
                session.add(Contact(phone=phone, name=name))
                session.commit()

    def get_contact_name(self, phone: str) -> Optional[str]:
        with get_session() as session:
            contact = session.get(Contact, phone)
            return contact.name if contact else None


memory = Memory()  # singleton sederhana, di-import oleh main.py & cli


# ---------------------------------------------------------------------------
# CHECKPOINT: python memory.py
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    from database import init_db, seed_db
    init_db()
    seed_db()

    phone = "6281200099999"

    print("=== 1. History awal (harus kosong) ===")
    print(f"  {len(memory.get_history(phone))} pesan")

    print("\n=== 2. Simpan 3 giliran percakapan ===")
    memory.add(phone, "user", "halo")
    memory.add(phone, "model", "Halo kak, selamat siang 😊")
    memory.add(phone, "user", "ada treatment buat jerawat?")
    h = memory.get_history(phone)
    print(f"  {len(h)} pesan, terakhir: role={h[-1].role!r} text={h[-1].parts[0].text!r}")

    print("\n=== 3. Sliding window (isi 30 pesan, ambil harus 20) ===")
    for i in range(30):
        memory.add(phone, "user" if i % 2 == 0 else "model", f"pesan ke-{i}")
    h = memory.get_history(phone)
    print(f"  {len(h)} pesan (window={MEMORY_WINDOW}), tertua yang tersisa: {h[0].parts[0].text!r}")

    print("\n=== 4. Saklar takeover ===")
    print(f"  awal: ai_enabled={memory.is_ai_enabled(phone)}")
    memory.set_ai_enabled(phone, False)
    print(f"  setelah handover: ai_enabled={memory.is_ai_enabled(phone)}")
    memory.set_ai_enabled(phone, True)
    print(f"  setelah admin selesai: ai_enabled={memory.is_ai_enabled(phone)}")

    print("\nCheckpoint memory.py selesai.")