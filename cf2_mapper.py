from cf2_data import CF2Data


def build_cf2_data(record):

    return CF2Data(

        transmittal=record.transmittal,
        patient_name=record.patient_name,
        doctor=record.doctor,
        accreditation_no=record.accreditation_no,
        first_treatment=record.first_treatment,
        last_treatment=record.last_treatment,
        total_sessions=record.total_sessions,
        member_pin=record.member_pin,
        session_dates=record.treatment_dates

    )