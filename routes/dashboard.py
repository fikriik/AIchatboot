import os
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from fastapi import APIRouter, Body, Depends, Header, HTTPException
from fastapi.responses import HTMLResponse
from sqlmodel import select

from database import Booking, Contact, ConversationMessage, Treatment, get_session

load_dotenv()

router = APIRouter()

DASHBOARD_API_KEY = os.getenv("DASHBOARD_API_KEY", "")
AI_NAME = os.getenv("AI_NAME", "Gita")
COMPANY_NAME = os.getenv("COMPANY_NAME", "Klinik")
TEMPLATE = Path(__file__).resolve().parent.parent / "templates" / "dashboard.html"

def require_key(x_api_key: str = Header(default="")) -> None:
    """Gerbang sederhana: tiap request API harus bawa X-API-Key yang benar."""
    if not DASHBOARD_API_KEY or x_api_key != DASHBOARD_API_KEY:
        raise HTTPException(status_code=401, detail="API key dashboard salah.")
    

# ---------------------------------------------------------------------------
# Halaman (HTML shell). Data diambil oleh JavaScript lewat /dashboard/api/*.
# ---------------------------------------------------------------------------
@router.get("/dashboard", response_class=HTMLResponse)
def dashboard_page() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


@router.get("/dashboard/api/config", dependencies=[Depends(require_key)])
def config():
    return {"company": COMPANY_NAME, "ai_name": AI_NAME}


# ---------------------------------------------------------------------------
# SECTION 1 — Conversations
# ---------------------------------------------------------------------------
@router.get("/dashboard/api/conversations", dependencies=[Depends(require_key)])
def list_conversations():
    """Satu baris per nomor: nama, pesan terakhir, status AI, ada booking atau tidak."""
    with get_session() as s:
        msgs = s.exec(
            select(ConversationMessage).order_by(ConversationMessage.created_at.desc())
        ).all()
        contacts = {c.phone: c for c in s.exec(select(Contact)).all()}
        booking_phones = {
            b.phone for b in s.exec(select(Booking).where(Booking.status != "cancelled")).all()
        }

        last: dict[str, ConversationMessage] = {}
        for m in msgs:                       # pesan sudah urut terbaru -> pertama = terakhir
            last.setdefault(m.phone, m)

        convos = []
        for phone, m in last.items():
            c = contacts.get(phone)
            convos.append({
                "phone": phone,
                "name": (c.name if c and c.name else phone),
                "last_text": m.text,
                "last_role": m.role,
                "last_time": m.created_at.isoformat(),
                "ai_enabled": (c.ai_enabled if c else True),
                "handover": bool(c and not c.ai_enabled),
                "has_booking": phone in booking_phones,
            })
        convos.sort(key=lambda x: x["last_time"], reverse=True)
        return convos
@router.post("/dashboard/api/conversations/{phone}/ai", dependencies=[Depends(require_key)])
def toggle_ai(phone: str, enabled: bool = Body(..., embed=True)):
    """Saklar takeover: matikan/nyalakan AI untuk satu nomor."""
    with get_session() as s:
        c = s.get(Contact, phone)
        if c:
            c.ai_enabled = enabled
        else:
            s.add(Contact(phone=phone, ai_enabled=enabled))
        s.commit()
    return {"phone": phone, "ai_enabled": enabled}


@router.post("/dashboard/api/conversations/{phone}/send", dependencies=[Depends(require_key)])
async def admin_send(phone: str, text: str = Body(..., embed=True)):
    """Admin membalas manual. Pesan dikirim ke WhatsApp + dicatat sebagai pesan klinik."""
    from memory import memory
    from whatsapp import send_reply

    text = (text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Pesan kosong.")
    await send_reply(phone, text)
    memory.add(phone, "model", text)
    return {"ok": True}

# ---------------------------------------------------------------------------
# SECTION 2 — Bookings
# ---------------------------------------------------------------------------
VALID_STATUS = {"pending", "confirmed", "completed", "cancelled"}


@router.get("/dashboard/api/bookings", dependencies=[Depends(require_key)])
def list_bookings(range: str = "all", status: str = "all", q: str = ""):
    """Daftar booking + statistik. Filter: range tanggal, status, kata kunci."""
    with get_session() as s:
        rows = s.exec(
            select(Booking, Treatment)
            .join(Treatment, Booking.treatment_id == Treatment.id)
            .order_by(Booking.booking_date.desc(), Booking.booking_time)
        ).all()

        today = datetime.now().date()
        all_b = [b for b, _ in rows]
        stats = {
            "today": sum(1 for b in all_b if b.booking_date == today and b.status != "cancelled"),
            "total": len(all_b),
            "active": sum(1 for b in all_b if b.status in ("pending", "confirmed")),
            "completed": sum(1 for b in all_b if b.status == "completed"),
        }

        days_map = {"7": 7, "30": 30, "90": 90}

        def keep(b, t) -> bool:
            if status != "all" and b.status != status:
                return False
            if range == "today" and b.booking_date != today:
                return False
            if range in days_map:
                d = days_map[range]
                if not (today - timedelta(days=d) <= b.booking_date <= today + timedelta(days=d)):
                    return False
            if q:
                ql = q.lower()
                if ql not in b.customer_name.lower() and ql not in t.name.lower():
                    return False
            return True

        items = [
            {"code": b.code, "customer_name": b.customer_name, "phone": b.phone,
             "treatment": t.name, "date": str(b.booking_date),
             "time": b.booking_time.strftime("%H:%M"), "status": b.status}
            for b, t in rows if keep(b, t)
        ]
        return {"stats": stats, "bookings": items}


@router.post("/dashboard/api/bookings/{code}/status", dependencies=[Depends(require_key)])
def update_booking_status(code: str, status: str = Body(..., embed=True)):
    """Ubah status booking (pending/confirmed/completed/cancelled)."""
    if status not in VALID_STATUS:
        raise HTTPException(status_code=400, detail="Status tidak valid.")
    with get_session() as s:
        b = s.exec(select(Booking).where(Booking.code == code)).first()
        if not b:
            raise HTTPException(status_code=404, detail="Booking tidak ditemukan.")
        b.status = status
        s.commit()
    return {"code": code, "status": status}