from dataclasses import dataclass, field
from datetime import date


@dataclass
class PatientRecord:

    transmittal: str

    patient_name: str

    doctor: str

    accreditation_no: str

    treatment_dates_raw: str

    member_pin: str = ""

    treatment_dates: list[date] = field(default_factory=list)

    first_treatment: date | None = None

    last_treatment: date | None = None

    total_sessions: int = 0