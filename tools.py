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
