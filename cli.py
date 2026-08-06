
from database import init_db, seed_db
from agent import run_agent, MODEL

DUMMY_PHONE = "6281234567890"  # di WhatsApp nanti, ini nomor pengirim asli


def main() -> None:
    init_db()
    seed_db()

    print("=" * 60)
    print(f"  Glowria Aesthetic Clinic — AI Sales Agent (model: {MODEL})")
    print("  Ketik pesan seperti customer WhatsApp. 'exit' untuk keluar.")
    print("=" * 60)

    history: list = []
    while True:
        try:
            user_input = input("\nCustomer > ").strip()
        except (KeyboardInterrupt, EOFError):
            break
        if not user_input or user_input.lower() in {"exit", "quit"}:
            break

        reply, history = run_agent(history, user_input, phone=DUMMY_PHONE)

        # Tampilkan seperti bubble WhatsApp: pecah di [NEXT], stop di [WAIT]
        clean = reply.replace("[WAIT]", "").replace("[HANDOVER]", "\n  ⚠ [HANDOVER terdeteksi]")
        for bubble in clean.split("[NEXT]"):
            bubble = bubble.strip()
            if bubble:
                print(f"\nGita    > {bubble}")

    print("\nSampai jumpa!")


if __name__ == "__main__":
    main()