import random
import string
from datetime import datetime, date, time, timedelta
from typing import Optional

from sqlmodel import select

from database import (
    Booking,
    ClinicHours,
    Contact,
    DoctorSchedule,
    MAX_CONCURRENT,
    SpecialSchedule,
    Treatment,
    TREATMENT_DURATION_MINUTES,
    get_session,
)

HARI = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()

def _parse_time(s: str) -> time:
    return datetime.strptime(s, "%H:%M").time()

def _minutes(t: time) -> int:
    return t.hour * 60 + t.minute


def _to_time(minutes: int) -> time:
    return time(minutes // 60, minutes % 60)


def _get_day_rules(session, d: date) -> dict:
    """Gabungkan jam reguler + JADWAL KHUSUS untuk satu tanggal.

    Return: {is_closed, open, close, doctor_name, doctor_start, doctor_end, note}
    SpecialSchedule selalu menang (override prioritas tertinggi, sesuai prompt).
    """
    hours = session.get(ClinicHours, d.weekday())
    result = {
        "is_closed": hours is None,
        "open": hours.open_time if hours else None,
        "close": hours.close_time if hours else None,
        "doctor_name": None,
        "doctor_start": None,
        "doctor_end": None,
        "note": None,
    }

    doc = session.exec(
        select(DoctorSchedule).where(DoctorSchedule.weekday == d.weekday())
    ).first()
    if doc:
        result.update(doctor_name=doc.doctor_name,
                    doctor_start=doc.start_time, doctor_end=doc.end_time)

    special = session.exec(
        select(SpecialSchedule).where(SpecialSchedule.schedule_date == d)
    ).first()
    if special:
        result["note"] = special.note
        if special.is_closed:
            result["is_closed"] = True
        if special.open_time:
            result["open"] = special.open_time
        if special.close_time:
            result["close"] = special.close_time
        if special.doctor_name:
            result.update(doctor_name=special.doctor_name,
                        doctor_start=special.doctor_start,
                        doctor_end=special.doctor_end)
    return result

def _count_overlap(session, d: date, start: time,
                duration: int = TREATMENT_DURATION_MINUTES) -> int:
    """Hitung booking aktif yang OVERLAP dengan rentang [start, start+duration).

    Dua rentang [a1,a2) dan [b1,b2) overlap jika a1 < b2 dan b1 < a2.
    Inilah jantung capacity-based booking.
    """
    s1, e1 = _minutes(start), _minutes(start) + duration
    bookings = session.exec(
        select(Booking).where(
            Booking.booking_date == d,
            Booking.status != "cancelled",
        )
    ).all()
    count = 0
    for b in bookings:
        s2 = _minutes(b.booking_time)
        e2 = s2 + b.duration_minutes
        if s1 < e2 and s2 < e1:
            count += 1
    return count

def _next_available_slot(session, d: date, after: time, rules: dict) -> Optional[str]:
    """Cari waktu tersedia TERDEKAT setelah `after` (langkah 30 menit).

    Sesuai NEAREST SLOT RULE di prompt: jangan lompati slot kosong.
    Slot valid jika start + 90 menit <= jam tutup (LAST VALID SLOT rule).
    """
    last_valid = _minutes(rules["close"]) - TREATMENT_DURATION_MINUTES
    m = _minutes(after) + 30
    m += (30 - m % 30) % 30  # bulatkan naik ke kelipatan 30
    while m <= last_valid:
        if _count_overlap(session, d, _to_time(m)) < MAX_CONCURRENT:
            return f"{m // 60:02d}:{m % 60:02d}"
        m += 30
    return None

def _generate_code() -> str:
    suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
    return f"GLW-{datetime.now():%Y%m%d}-{suffix}"

def search_treatments(query: str) -> dict:
    """Cari treatment by nama atau kategori (case-insensitive)."""
    q = f"%{query.lower()}%"
    with get_session() as session:
        rows = session.exec(
            select(Treatment).where(
                Treatment.name.ilike(q) | Treatment.category.ilike(q)
                | Treatment.description.ilike(q)
            )
        ).all()
        if not rows:
            return {"found": False,
                    "message": f"Tidak ada treatment yang cocok dengan '{query}'."}
        return {
            "found": True,
            "treatments": [
                {
                    "id": t.id,
                    "name": t.name,
                    "price": t.price,
                    "duration_minutes": TREATMENT_DURATION_MINUTES,
                    "description": t.description,
                    "promo": t.promo,
                    "requires_doctor": t.requires_doctor,
                }
                for t in rows
            ],
        }
    
def check_available_schedule(booking_date: str) -> dict:
    """Cek slot tersedia untuk satu tanggal (MODE B di prompt).

    Mengembalikan jam buka, dokter bertugas, slot per 30 menit dengan
    sisa kapasitas, dan next_available_slot.
    """
    d = _parse_date(booking_date)
    with get_session() as session:
        rules = _get_day_rules(session, d)
        if rules["is_closed"]:
            return {"status": "CLINIC_CLOSED", "date": booking_date,
                    "day": HARI[d.weekday()], "note": rules["note"]}

        last_valid = _minutes(rules["close"]) - TREATMENT_DURATION_MINUTES
        slots = []
        m = _minutes(rules["open"])

        # Untuk booking HARI INI: lewati slot yang jamnya sudah berlalu.
        now = datetime.now()
        if d == now.date():
            now_m = _minutes(now.time())
            if m < now_m:
                m = now_m + (30 - now_m % 30) % 30  # mulai dari slot :00/:30 berikutnya

        while m <= last_valid:
            t = _to_time(m)
            used = _count_overlap(session, d, t)
            if used < MAX_CONCURRENT:
                slots.append({
                    "start": f"{t:%H:%M}",
                    "end": f"{_to_time(m + TREATMENT_DURATION_MINUTES):%H:%M}",
                    "remaining_capacity": MAX_CONCURRENT - used,
                })
            m += 30

        doctor = None
        if rules["doctor_name"]:
            doctor = {"name": rules["doctor_name"],
                    "start": f"{rules['doctor_start']:%H:%M}",
                    "end": f"{rules['doctor_end']:%H:%M}"}

        return {
            "status": "OK",
            "date": booking_date,
            "day": HARI[d.weekday()],
            "open": f"{rules['open']:%H:%M}",
            "close": f"{rules['close']:%H:%M}",
            "last_valid_slot": f"{_to_time(last_valid):%H:%M}",
            "doctor_on_duty": doctor,
            "available_slots": slots,
            "all_full": len(slots) == 0,
            "max_concurrent": MAX_CONCURRENT,
            "note": rules["note"],
        }

def book_treatment(customer_name: str, phone: str, treatment_name: str,
                booking_date: str, booking_time: str) -> dict:
    """Buat booking — SINGLE SOURCE OF TRUTH (MODE A di prompt).

    Cek ulang semuanya secara atomik di sini:
    1. Klinik buka? (termasuk JADWAL KHUSUS)
    2. Jam valid? (dalam jam buka, start + 90 <= tutup)
    3. Kapasitas masih ada? (overlap < MAX_CONCURRENT)
    Kalau konflik -> SLOT_CONFLICT + next_available_slot (jangan pernah
    biarkan AI mengarang jam alternatif sendiri).
    """
    d = _parse_date(booking_date)
    t = _parse_time(booking_time)

    with get_session() as session:
        rules = _get_day_rules(session, d)

        if rules["is_closed"]:
            return {"success": False, "error": "CLINIC_CLOSED",
                    "message": f"Klinik tutup pada {HARI[d.weekday()]}, {booking_date}."
                            + (f" Catatan: {rules['note']}" if rules["note"] else "")}

        start_m = _minutes(t)

        # TOLAK WAKTU LAMPAU: tanggal sudah lewat, atau jam hari ini sudah lewat.
        now = datetime.now()
        if d < now.date() or (d == now.date() and start_m <= _minutes(now.time())):
            return {"success": False, "error": "PAST_TIME",
                    "message": (f"Jam {booking_time} pada {booking_date} sudah lewat. "
                                "Tidak bisa booking untuk waktu yang sudah berlalu."),
                    "next_available_slot": _next_available_slot(session, d, now.time(), rules)
                                        if d == now.date() else None}

        last_valid = _minutes(rules["close"]) - TREATMENT_DURATION_MINUTES
        if start_m < _minutes(rules["open"]) or start_m > last_valid:
            return {"success": False, "error": "OUTSIDE_HOURS",
                    "message": (f"Jam {booking_time} tidak valid. Klinik buka "
                                f"{rules['open']:%H:%M}-{rules['close']:%H:%M}, "
                                f"slot terakhir {_to_time(last_valid):%H:%M} "
                                f"(treatment {TREATMENT_DURATION_MINUTES} menit harus selesai "
                                f"sebelum tutup).")}

        treatment = session.exec(
            select(Treatment).where(Treatment.name.ilike(f"%{treatment_name}%"))
        ).first()
        if not treatment:
            return {"success": False, "error": "TREATMENT_NOT_FOUND",
                    "message": f"Treatment '{treatment_name}' tidak ditemukan. "
                            "Gunakan search_treatments untuk daftar yang tersedia."}

        # CEK KAPASITAS — di transaksi yang sama dengan insert (race protection)
        used = _count_overlap(session, d, t)
        if used >= MAX_CONCURRENT:
            return {"success": False, "error": "SLOT_CONFLICT",
                    "message": f"Jam {booking_time} sudah penuh ({used}/{MAX_CONCURRENT}).",
                    "next_available_slot": _next_available_slot(session, d, t, rules)}

        booking = Booking(
            code=_generate_code(),
            customer_name=customer_name,
            phone=phone,
            treatment_id=treatment.id,
            booking_date=d,
            booking_time=t,
        )
        session.add(booking)

        # Update/insert contact: setelah booking, customer dianggap returning
        contact = session.get(Contact, phone)
        if contact:
            contact.name = contact.name or customer_name
            contact.is_returning = True
        else:
            session.add(Contact(phone=phone, name=customer_name, is_returning=True))

        session.commit()
        session.refresh(booking)

        doctor_info = None
        if rules["doctor_name"]:
            ds, de = _minutes(rules["doctor_start"]), _minutes(rules["doctor_end"])
            on_duty = ds <= start_m < de
            doctor_info = {"name": rules["doctor_name"], "on_duty_at_booking_time": on_duty,
                        "hours": f"{rules['doctor_start']:%H:%M}-{rules['doctor_end']:%H:%M}"}

        return {
            "success": True,
            "booking_code": booking.code,
            "customer_name": customer_name,
            "treatment": treatment.name,
            "price": treatment.price,
            "date": booking_date,
            "day": HARI[d.weekday()],
            "time": booking_time,
            "end_time": f"{_to_time(start_m + TREATMENT_DURATION_MINUTES):%H:%M}",
            "doctor": doctor_info,
            "status": "pending",
        }

def get_my_orders(phone: str) -> dict:
    """Riwayat booking customer berdasarkan nomor WhatsApp."""
    with get_session() as session:
        rows = session.exec(
            select(Booking, Treatment)
            .join(Treatment, Booking.treatment_id == Treatment.id)
            .where(Booking.phone == phone)
            .order_by(Booking.booking_date.desc())
        ).all()
        if not rows:
            return {"found": False, "message": "Tidak ada riwayat booking untuk nomor ini."}
        return {
            "found": True,
            "bookings": [
                {"booking_code": b.code, "treatment": t.name,
                "date": str(b.booking_date), "time": f"{b.booking_time:%H:%M}",
                "status": b.status}
                for b, t in rows
            ],
        }

def cancel_booking(booking_code: str, reason: str = "") -> dict:
    """Soft-cancel booking by kode."""
    with get_session() as session:
        booking = session.exec(
            select(Booking).where(Booking.code == booking_code)
        ).first()
        if not booking:
            return {"success": False, "error": "NOT_FOUND",
                    "message": f"Booking {booking_code} tidak ditemukan."}
        if booking.status == "cancelled":
            return {"success": False, "error": "ALREADY_CANCELLED",
                    "message": "Booking ini sudah dibatalkan sebelumnya."}
        booking.status = "cancelled"
        session.commit()
        return {"success": True, "booking_code": booking_code,
                "message": "Booking berhasil dibatalkan.", "reason": reason}
    

def handover_to_admin(phone: str, reason: str) -> dict:
    """Matikan AI untuk nomor ini + tandai perlu admin.

    Dipanggil saat: data tidak ada di sistem, komplain berat, customer minta
    bicara admin. Di main.py nanti, [HANDOVER] di output AI juga men-trigger ini.
    """
    with get_session() as session:
        contact = session.get(Contact, phone)
        if contact:
            contact.ai_enabled = False
        else:
            session.add(Contact(phone=phone, ai_enabled=False))
        session.commit()
    # Di production: kirim notifikasi ke grup admin WhatsApp / dashboard di sini
    return {"success": True, "ai_disabled_for": phone, "reason": reason,
            "message": "AI dinonaktifkan untuk nomor ini. Admin telah dinotifikasi."}


# ---------------------------------------------------------------------------
# FUNCTION DECLARATIONS untuk Gemini (google-genai SDK)
# Kualitas tool calling sangat bergantung pada deskripsi di bawah ini.
# ---------------------------------------------------------------------------


TOOL_DECLARATIONS = [
    {
        "name": "search_treatments",
        "description": ("Cari treatment, paket, atau layanan konsultasi yang tersedia di klinik "
                        "berdasarkan kata kunci (nama, kategori seperti 'acne'/'brightening', "
                        "atau keluhan). WAJIB dipanggil sebelum menyebut nama/harga treatment "
                        "apapun ke customer. Jangan pernah mengarang treatment dari ingatan."),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                        "description": "Kata kunci, contoh: 'acne', 'botox', 'konsultasi', 'brightening'"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "check_available_schedule",
        "description": ("Cek slot tersedia untuk satu tanggal (MODE B: HANYA saat customer belum "
                        "menyebut jam spesifik, atau saat butuh next_available_slot). Mengembalikan "
                        "jam buka/tutup, dokter yang bertugas, daftar slot dengan sisa kapasitas, "
                        "dan catatan jadwal khusus."),
        "parameters": {
            "type": "object",
            "properties": {
                "booking_date": {"type": "string",
                                "description": "Tanggal format YYYY-MM-DD, contoh: 2026-06-15"},
            },
            "required": ["booking_date"],
        },
    },
    {
        "name": "book_treatment",
        "description": ("Buat booking treatment/konsultasi. Ini SATU-SATUNYA konfirmasi final "
                        "ketersediaan slot (atomic check). Panggil HANYA setelah semua data "
                        "lengkap dan customer konfirmasi recap. Jika hasilnya SLOT_CONFLICT, "
                        "tawarkan next_available_slot dari hasil tool, JANGAN mengarang jam."),
        "parameters": {
            "type": "object",
            "properties": {
                "customer_name": {"type": "string", "description": "Nama customer"},
                "phone": {"type": "string",
                        "description": "Nomor WhatsApp customer (otomatis dari konteks, jangan tanya)"},
                "treatment_name": {"type": "string",
                                "description": "Nama treatment persis dari hasil search_treatments"},
                "booking_date": {"type": "string", "description": "Format YYYY-MM-DD"},
                "booking_time": {"type": "string", "description": "Format HH:MM, contoh: 14:00"},
            },
            "required": ["customer_name", "phone", "treatment_name", "booking_date", "booking_time"],
        },
    },
    {
        "name": "get_my_orders",
        "description": ("Ambil riwayat booking customer berdasarkan nomor WhatsApp. Dipakai saat "
                        "customer tanya booking mereka, mau reschedule, atau mau cancel. "
                        "Jika data tidak ditemukan padahal customer yakin pernah booking -> handover."),
        "parameters": {
            "type": "object",
            "properties": {
                "phone": {"type": "string", "description": "Nomor WhatsApp customer"},
            },
            "required": ["phone"],
        },
    },
    {
        "name": "cancel_booking",
        "description": "Batalkan booking berdasarkan kode booking (dapatkan dari get_my_orders dulu).",
        "parameters": {
            "type": "object",
            "properties": {
                "booking_code": {"type": "string", "description": "Kode booking, contoh: GLW-20260613-A3F1"},
                "reason": {"type": "string", "description": "Alasan pembatalan dari customer"},
            },
            "required": ["booking_code"],
        },
    },
    {
        "name": "handover_to_admin",
        "description": ("Alihkan percakapan ke admin manusia dan nonaktifkan AI untuk nomor ini. "
                        "Panggil saat: customer minta bicara admin, komplain berat, data yang "
                        "ditanya tidak ada di sistem (maksimal 1x coba tool lain dulu), atau "
                        "situasi di luar kemampuan."),
        "parameters": {
            "type": "object",
            "properties": {
                "phone": {"type": "string", "description": "Nomor WhatsApp customer"},
                "reason": {"type": "string", "description": "Alasan handover untuk dicatat ke admin"},
            },
            "required": ["phone", "reason"],
        },
    },
]



TOOL_FUNCTIONS = {
    "search_treatments": search_treatments,
    "check_available_schedule": check_available_schedule,
    "book_treatment": book_treatment,
    "get_my_orders": get_my_orders,
    "cancel_booking": cancel_booking,
    "handover_to_admin": handover_to_admin,
}

if __name__ == "__main__":
    from database import init_db, seed_db
    init_db()
    seed_db()

    print("=== 1. search_treatments('acne') ===")
    r = search_treatments("acne")
    for t in r["treatments"]:
        print(f"  {t['name']} - Rp {t['price']:,}")

    print("\n=== 2. Booking Senin 15 Juni jam 14:00 (3x, kapasitas 3) ===")
    for nama in ["Rani", "Sari", "Dewi"]:
        r = book_treatment(nama, f"62812000{nama}", "Facial Acne", "2026-06-15", "14:00")
        print(f"  {nama}: success={r['success']} code={r.get('booking_code')}")

    print("\n=== 3. Booking ke-4 di jam yang sama -> harus SLOT_CONFLICT ===")
    r = book_treatment("Putu", "62812000Putu", "Facial Acne", "2026-06-15", "14:00")
    print(f"  success={r['success']} error={r.get('error')}")
    print(f"  next_available_slot={r.get('next_available_slot')} (harus 15:30: overlap 14:00+90mnt baru lepas)")

    print("\n=== 4. Booking jam 17:00 -> OUTSIDE_HOURS (selesai 18:30 > tutup 18:00) ===")
    r = book_treatment("Putu", "62812000Putu", "Facial Acne", "2026-06-15", "17:00")
    print(f"  success={r['success']} error={r.get('error')}")

    print("\n=== 5. Booking 25 Juni -> CLINIC_CLOSED (jadwal khusus) ===")
    r = book_treatment("Putu", "62812000Putu", "Facial Acne", "2026-06-25", "14:00")
    print(f"  success={r['success']} error={r.get('error')}")
    print(f"  message={r.get('message')}")

    print("\n=== 6. check_available_schedule 22 Juni (dokter geser ke 14:30) ===")
    r = check_available_schedule("2026-06-22")
    print(f"  dokter: {r['doctor_on_duty']}")
    print(f"  note: {r['note']}")
    print(f"  slot tersedia: {len(r['available_slots'])} (pertama {r['available_slots'][0]['start']}, "
        f"terakhir {r['available_slots'][-1]['start']})")

    print("\n=== 7. get_my_orders + cancel ===")
    r = get_my_orders("62812000Rani")
    code = r["bookings"][0]["booking_code"]
    print(f"  riwayat Rani: {r['bookings']}")
    r = cancel_booking(code, "berhalangan")
    print(f"  cancel {code}: success={r['success']}")

    print("\n=== 8. Setelah 1 cancel, jam 14:00 terbuka lagi (2/3 terisi) ===")
    r = book_treatment("Putu", "62812000Putu", "Facial Acne", "2026-06-15", "14:00")
    print(f"  success={r['success']} code={r.get('booking_code')}")

    print("\n=== 9. handover_to_admin ===")
    r = handover_to_admin("62812000Putu", "customer minta bicara admin")
    print(f"  {r['message']}")

    print("\nSemua skenario checkpoint selesai.")