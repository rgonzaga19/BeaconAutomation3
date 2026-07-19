from dataclasses import dataclass, field
from datetime import date
from typing import List


@dataclass
class CF2Data:

    transmittal: str
    patient_name: str
    doctor: str
    accreditation_no: str
    first_treatment: date
    last_treatment: date
    total_sessions: int
    member_pin: str = ""
    session_dates: List[date] = field(default_factory=list)