import os
from datetime import datetime, date, time
from typing import Optional

from dotenv import load_dotenv
from sqlmodel import SQLModel, Field, Session, create_engine, select
from sqlalchemy import event

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///clinic.db")
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args, echo=False)

if DATABASE_URL.startswith("sqlite"):

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

TREATMENT_DURATION_MINUTES = 90   # 1 treatment = 90 menit (aturan absolut di prompt)
MAX_CONCURRENT = 3                # 3 perawat paralel (capacity-based booking)

class Treatment(SQLModel, table=True):
    """Daftar treatment yang tersedia di klinik."""

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    category: str = Field(index=True)        # konsultasi | acne | brightening | anti-aging | hair-removal
    price: int
    description: str
    promo: Optional[str] = None
    requires_doctor: bool = Field(default=False)


class Booking(SQLModel, table=True):
    """Booking treatment. Konflik dihitung dari OVERLAP, bukan jam eksak.

    Slot penuh = jumlah booking yang overlap dengan rentang
    [start, start+90mnt) sudah mencapai MAX_CONCURRENT.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    code: str = Field(index=True, unique=True)   # mis. "GLW-20260613-A3F1"
    customer_name: str
    phone: str = Field(index=True)
    treatment_id: int = Field(foreign_key="treatment.id")
    booking_date: date = Field(index=True)
    booking_time: time
    duration_minutes: int = Field(default=TREATMENT_DURATION_MINUTES)
    status: str = Field(default="pending", index=True)  # pending | confirmed | cancelled
    created_at: datetime = Field(default_factory=datetime.now)

class ClinicHours(SQLModel, table=True):
    """Jam operasional reguler per hari. weekday: 0=Senin ... 6=Minggu."""

    weekday: int = Field(primary_key=True)
    open_time: time
    close_time: time

class SpecialSchedule(SQLModel, table=True):
    """JADWAL KHUSUS — override prioritas tertinggi untuk tanggal tertentu.

    Dipakai untuk: klinik tutup total, jam dokter geser, jam buka berubah.
    Tool checkAvailableSchedule & bookTreatment WAJIB cek tabel ini dulu.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    schedule_date: date = Field(index=True, unique=True)
    is_closed: bool = Field(default=False)            # True = KLINIK TUTUP TOTAL
    open_time: Optional[time] = None                  # None = ikut jam reguler
    close_time: Optional[time] = None
    doctor_name: Optional[str] = None                 # override jadwal dokter hari itu
    doctor_start: Optional[time] = None
    doctor_end: Optional[time] = None
    note: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.now)

class DoctorSchedule(SQLModel, table=True):
    """Jadwal dokter reguler per hari. weekday: 0=Senin ... 6=Minggu."""

    id: Optional[int] = Field(default=None, primary_key=True)
    doctor_name: str = Field(index=True)
    weekday: int = Field(index=True)
    start_time: time
    end_time: time

class ConversationMessage(SQLModel, table=True):
    """History chat per nomor. Dipakai memory AI + dibaca dashboard."""

    id: Optional[int] = Field(default=None, primary_key=True)
    phone: str = Field(index=True)
    role: str  # "user" | "model"
    text: str
    created_at: datetime = Field(default_factory=datetime.now, index=True)

class ConversationMessage(SQLModel, table=True):
    """History chat per nomor. Dipakai memory AI + dibaca dashboard."""

    id: Optional[int] = Field(default=None, primary_key=True)
    phone: str = Field(index=True)
    role: str  # "user" | "model"
    text: str
    created_at: datetime = Field(default_factory=datetime.now, index=True)

class Contact(SQLModel, table=True):
    """Satu baris per nomor WhatsApp.

    ai_enabled = saklar takeover. Di-set False saat [HANDOVER] terdeteksi,
    admin yang menyalakan lagi dari dashboard.
    """

    phone: str = Field(primary_key=True)
    name: Optional[str] = None
    ai_enabled: bool = Field(default=True)
    is_returning: bool = Field(default=False)  # pernah treatment? (New vs Returning di prompt)
    created_at: datetime = Field(default_factory=datetime.now)

# --- mulai dari sini lumayan banyak kalau diketik

SEED_TREATMENTS = [
    # Konsultasi (dua jenis — pembedaan penting di prompt!)
    Treatment(name="Konsultasi Treatment", category="konsultasi", price=0,
            description="Konsultasi GRATIS untuk memilih treatment kecantikan yang tepat.",
            requires_doctor=True),
    Treatment(name="Konsultasi Penyakit Kulit & Kelamin", category="konsultasi", price=125_000,
            description="Konsultasi kondisi medis, penyakit kulit, dan prosedur dokter spesialis.",
            requires_doctor=True),
    # Acne
    Treatment(name="Facial Acne", category="acne", price=100_000,
            description="Pembersihan mendalam untuk kulit berjerawat.",
            promo="Promo Pelajar Rp 100.000 (tunjukkan kartu pelajar)"),
    Treatment(name="Paket Acne A (Peeling Acne + Meso Purifying)", category="acne", price=275_000,
            description="Kombinasi peeling dan meso untuk jerawat aktif.", promo="Diskon 50% s/d 30 Juni"),
    Treatment(name="Paket Acne B (IPL Acne + Meso Purifying)", category="acne", price=425_000,
            description="IPL untuk peradangan jerawat plus meso purifying.", promo="Diskon 50% s/d 30 Juni"),
    # Brightening
    Treatment(name="Paket Brightening A (Facial Whitening Premium + Infus Brightening)",
            category="brightening", price=495_000,
            description="Mencerahkan kulit dari luar dan dalam.", promo="Diskon 40% s/d 30 Juni"),
    Treatment(name="Infus Brightening 2x", category="brightening", price=695_000,
            description="Paket dua sesi infus vitamin untuk mencerahkan kulit.", promo="Diskon 40% s/d 30 Juni"),
    # Anti-aging & contouring (butuh dokter)
    Treatment(name="Glowtox (Skinbooster + Botox)", category="anti-aging", price=1_250_000,
            description="Treatment baru: skinbooster + botox untuk pori dan minyak.",
            promo="Hemat 50% (normal Rp 2.500.000)", requires_doctor=True),
    Treatment(name="Botox Allergan Full Face", category="anti-aging", price=4_800_000,
            description="Botox full face dengan produk Allergan.",
            promo="Hemat 38% (normal Rp 7.800.000)", requires_doctor=True),
    # Hair removal
    Treatment(name="IPL Hair Removal Underarm 4x", category="hair-removal", price=475_000,
            description="Paket 4 sesi hair removal area ketiak.", promo="Diskon 50% s/d 30 Juni"),
]

# weekday: 0=Senin ... 6=Minggu — sesuai <clinic_hours> di prompt
SEED_HOURS = [
    *[ClinicHours(weekday=d, open_time=time(10, 0), close_time=time(18, 0)) for d in range(6)],
    ClinicHours(weekday=6, open_time=time(10, 0), close_time=time(16, 0)),
]

# Sesuai <clinic_doctors>: Dr. Amara Sen/Rab/Jum 12-16, Dr. Sinta Sel/Kam/Sab 11-17, Minggu 11-15
SEED_DOCTORS = [
    *[DoctorSchedule(doctor_name="Dr. Amara, SpDVE", weekday=d, start_time=time(12, 0), end_time=time(16, 0))
    for d in (0, 2, 4)],
    *[DoctorSchedule(doctor_name="Dr. Sinta", weekday=d, start_time=time(11, 0), end_time=time(17, 0))
    for d in (1, 3, 5)],
    DoctorSchedule(doctor_name="Dr. Sinta", weekday=6, start_time=time(11, 0), end_time=time(15, 0)),
]

# Sesuai JADWAL KHUSUS di prompt (override prioritas tertinggi)
SEED_SPECIAL = [
    SpecialSchedule(schedule_date=date(2026, 6, 22), doctor_name="Dr. Amara, SpDVE",
                    doctor_start=time(14, 30), doctor_end=time(16, 0),
                    note="Dokter hanya praktek 1,5 jam. Sarankan jam 14:30 ke atas untuk treatment dokter."),
    SpecialSchedule(schedule_date=date(2026, 6, 24), is_closed=True, note="KLINIK TUTUP TOTAL"),
    SpecialSchedule(schedule_date=date(2026, 6, 25), is_closed=True, note="KLINIK TUTUP TOTAL"),
    SpecialSchedule(schedule_date=date(2026, 6, 26), is_closed=True, note="KLINIK TUTUP TOTAL"),
    SpecialSchedule(schedule_date=date(2026, 6, 27), doctor_name="Dr. Sinta",
                    doctor_start=time(12, 0), doctor_end=time(17, 0),
                    note="Dokter mulai lebih siang. Treatment butuh dokter: sarankan jam 12:00 ke atas."),
]

def init_db() -> None:
    """Buat semua tabel (idempotent)."""
    SQLModel.metadata.create_all(engine)

def seed_db() -> None:
    """Isi data dummy, hanya kalau masih kosong."""
    with Session(engine) as session:
        if session.exec(select(Treatment)).first():
            print("Seed dilewati: database sudah berisi data.")
            return
        session.add_all(SEED_TREATMENTS)
        session.add_all(SEED_HOURS)
        session.add_all(SEED_DOCTORS)
        session.add_all(SEED_SPECIAL)
        session.commit()
        print(f"Seed berhasil: {len(SEED_TREATMENTS)} treatment, jam operasional, "
            f"{len(SEED_DOCTORS)} jadwal dokter, {len(SEED_SPECIAL)} jadwal khusus.")


def get_session() -> Session:
    """Helper untuk tools.py dan routes (FastAPI dependency-friendly)."""
    return Session(engine)

# ---------------------------------------------------------------------------
# 5. CHECKPOINT: python database.py
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    init_db()
    seed_db()

    hari = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]
    with get_session() as session:
        treatments = session.exec(select(Treatment)).all()
        print(f"\nDatabase siap: {DATABASE_URL}")
        print(f"Kapasitas: {MAX_CONCURRENT} perawat paralel, durasi slot {TREATMENT_DURATION_MINUTES} menit")
        print(f"\nTreatment ({len(treatments)}):")
        for t in treatments:
            promo = f" [PROMO: {t.promo}]" if t.promo else ""
            dokter = " (butuh dokter)" if t.requires_doctor else ""
            print(f"  - {t.name}: Rp {t.price:,}{dokter}{promo}")

        print("\nJadwal dokter reguler:")
        for d in session.exec(select(DoctorSchedule).order_by(DoctorSchedule.weekday)).all():
            print(f"  - {hari[d.weekday]}: {d.doctor_name} {d.start_time}–{d.end_time}")

        print("\nJadwal khusus (override):")
        for s in session.exec(select(SpecialSchedule).order_by(SpecialSchedule.schedule_date)).all():
            status = "TUTUP TOTAL" if s.is_closed else f"{s.doctor_name} {s.doctor_start}–{s.doctor_end}"
            print(f"  - {s.schedule_date}: {status}")