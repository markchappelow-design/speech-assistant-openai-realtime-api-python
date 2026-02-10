import os
from datetime import datetime, timezone

import psycopg
from twilio.rest import Client

DATABASE_URL = os.environ["DATABASE_URL"]
TWILIO_ACCOUNT_SID = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN = os.environ["TWILIO_AUTH_TOKEN"]
TWILIO_FROM_NUMBER = os.environ["TWILIO_FROM_NUMBER"]
ALLOWED_TO_NUMBER = os.environ["ALLOWED_TO_NUMBER"]
PUBLIC_BASE_URL = os.environ["PUBLIC_BASE_URL"]  # e.g. https://speech-assistant-...onrender.com


def main():
    now = datetime.now(timezone.utc)
    twilio = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

    with psycopg.connect(DATABASE_URL, sslmode="require") as conn:
        with conn.cursor() as cur:
            # Grab due jobs and lock them so concurrent runs don't double-dial
            cur.execute(
                """
                select id
                from scheduled_calls
                where status = 'pending' and run_at_utc <= %s
                order by run_at_utc asc
                for update skip locked
                limit 5
                """,
                (now,),
            )
            ids = [r[0] for r in cur.fetchall()]

            for call_id in ids:
                try:
                    twilio.calls.create(
                        to=ALLOWED_TO_NUMBER,
                        from_=TWILIO_FROM_NUMBER,
                        url=f"{PUBLIC_BASE_URL}/outbound-call",
                        method="POST",
                    )
                    cur.execute(
                        "update scheduled_calls set status='triggered', last_error=null where id=%s",
                        (call_id,),
                    )
                except Exception as e:
                    cur.execute(
                        "update scheduled_calls set status='failed', last_error=%s where id=%s",
                        (str(e)[:800], call_id),
                    )

            conn.commit()


if __name__ == "__main__":
    main()
