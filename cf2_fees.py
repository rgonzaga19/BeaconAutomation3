# cf2_fees.py
# Lookup table for Hospital and Professional Fees based on number of claims

FEES_TABLE = {
    1: {"hospital_actual": 7500,  "hospital_discount": 6000,  "prof_actual": 437.5,  "prof_discount": 350},
    2: {"hospital_actual": 15000, "hospital_discount": 12000, "prof_actual": 875,    "prof_discount": 700},
    3: {"hospital_actual": 22500, "hospital_discount": 18000, "prof_actual": 1312.5, "prof_discount": 1050},
    4: {"hospital_actual": 30000, "hospital_discount": 24000, "prof_actual": 1750,   "prof_discount": 1400},
    5: {"hospital_actual": 37500, "hospital_discount": 30000, "prof_actual": 2187.5, "prof_discount": 1750},
    6: {"hospital_actual": 45000, "hospital_discount": 36000, "prof_actual": 2625,   "prof_discount": 2100},
    7: {"hospital_actual": 52500, "hospital_discount": 42000, "prof_actual": 3062.5, "prof_discount": 2450},
}


def get_fees(total_sessions: int) -> dict:
    if total_sessions not in FEES_TABLE:
        raise ValueError(f"No fee entry for {total_sessions} sessions. Update FEES_TABLE in cf2_fees.py.")
    return FEES_TABLE[total_sessions]