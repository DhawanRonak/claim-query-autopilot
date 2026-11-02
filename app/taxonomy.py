"""Document taxonomy shared by the locker, the query parser and the matcher.

Every locker document and every query point is mapped to one of these codes.
Matching is then a deterministic lookup, which keeps the "don't ask for what we
already have" rule auditable instead of leaving it to the LLM.

source:
  patient  -> only the patient can provide it, so we message them
  hospital -> the hospital/care.fi team holds it; never ask the patient
"""
import re

DOC_TYPES = {
    "discharge_summary":    {"label": "Discharge summary", "source": "hospital",
                             "keywords": ["discharge summary", "discharge card", "discharge certificate"]},
    "final_bill":           {"label": "Final hospital bill", "source": "hospital",
                             "keywords": ["final bill", "hospital bill", "final hospital bill", "interim bill", "bill breakup", "itemised bill", "itemized bill", "detailed bill", "break-up of bill", "breakup of bill"]},
    "pharmacy_bills":       {"label": "Pharmacy bills", "source": "patient",
                             "keywords": ["pharmacy bill", "medicine bill", "chemist bill", "pharmacy invoice", "drug bill"]},
    "investigation_reports": {"label": "Investigation reports", "source": "hospital",
                             "keywords": ["investigation report", "lab report", "laboratory report", "blood report", "x-ray", "xray", "ct scan", "mri", "ultrasound", "usg", "ecg", "histopathology", "biopsy", "pathology", "radiology", "angiography", "angiogram", "echo report", "2d echo", "imaging", "hba1c", "serology", "platelet", "ns1", "biometry", "a scan", "antenatal", "scan report", "test report", "blood alcohol"]},
    "consultation_papers":  {"label": "Consultation papers / prescriptions", "source": "patient",
                             "keywords": ["consultation paper", "consultation note", "prescription", "opd paper", "opd record", "first consultation", "doctor's advice", "referral letter"]},
    "past_medical_records": {"label": "Past medical / treatment records", "source": "patient",
                             "keywords": ["past medical", "previous treatment", "past treatment", "medical history", "previous records", "old records", "past history", "since when", "duration of ailment", "prior treatment", "pre-existing", "treatment record", "duration of", "first diagnosed", "previous hospitalisation", "previous hospitalization", "chronic"]},
    "policy_copy":          {"label": "Policy copy", "source": "patient",
                             "keywords": ["policy copy", "policy document", "policy schedule", "copy of policy", "policy certificate"]},
    "health_card":          {"label": "Health card / e-card", "source": "patient",
                             "keywords": ["health card", "e-card", "ecard", "tpa card", "id card of insured"]},
    "previous_policy_copies": {"label": "Previous years' policy copies", "source": "patient",
                             "keywords": ["previous policy", "previous year policy", "prior policy", "earlier policy", "portability", "continuity", "renewal history", "past policies", "policy history"]},
    "kyc_aadhaar":          {"label": "Aadhaar card (KYC)", "source": "patient",
                             "keywords": ["aadhaar", "aadhar", "uidai"]},
    "kyc_pan":              {"label": "PAN card (KYC)", "source": "patient",
                             "keywords": ["pan card", "pan copy", "permanent account number"]},
    "cancelled_cheque":     {"label": "Cancelled cheque", "source": "patient",
                             "keywords": ["cancelled cheque", "canceled cheque", "cancel cheque", "bank details", "neft", "bank account", "passbook"]},
    "claim_form":           {"label": "Signed claim form", "source": "patient",
                             "keywords": ["claim form", "signed claim form", "part a", "part-a"]},
    "fir_mlc":              {"label": "FIR / MLC copy", "source": "patient",
                             "keywords": ["fir", "mlc", "medico legal", "medico-legal", "police report", "first information report"]},
    "employer_certificate": {"label": "Employer / employee ID certificate", "source": "patient",
                             "keywords": ["employee id", "employer certificate", "employment proof", "hr letter", "group policy member"]},
    "indoor_case_papers":   {"label": "Indoor case papers", "source": "hospital",
                             "keywords": ["indoor case paper", "icp", "case sheet", "nursing notes", "progress notes", "treatment chart", "temperature chart"]},
    "ot_notes":             {"label": "OT / procedure notes", "source": "hospital",
                             "keywords": ["ot notes", "operation notes", "operative notes", "surgery notes", "procedure notes", "anaesthesia notes", "anesthesia notes"]},
    "implant_invoice":      {"label": "Implant invoice / sticker", "source": "hospital",
                             "keywords": ["implant", "stent", "sticker", "lens invoice", "iol"]},
    "treating_doctor_certificate": {"label": "Treating doctor's certificate", "source": "hospital",
                             "keywords": ["treating doctor", "doctor's certificate", "certificate from treating", "medical certificate", "justification", "line of treatment"]},
    "patient_declaration":  {"label": "Written declaration from patient", "source": "patient",
                             "keywords": ["declaration", "self declaration", "undertaking", "explain", "explanation", "reason for delay", "delay in intimation", "alcohol", "smoking", "habit", "occupation", "clarify", "clarification"]},
}

PATIENT_SOURCED = {k for k, v in DOC_TYPES.items() if v["source"] == "patient"}


def label(code: str) -> str:
    return DOC_TYPES.get(code, {}).get("label", code.replace("_", " ").title())


def guess_doc_type(text: str) -> str | None:
    """Best keyword guess for a filename or a chunk of text. Longest keyword wins."""
    t = (text or "").lower().replace("_", " ").replace("-", " ")
    best, best_len = None, 0
    for code, meta in DOC_TYPES.items():
        for kw in meta["keywords"]:
            k = kw.replace("-", " ")
            if len(k) > best_len and re.search(r"(?<![a-z])" + re.escape(k) + r"(?:s|es)?(?![a-z])", t):
                best, best_len = code, len(k)
    return best


def all_doc_types_in(text: str) -> list[str]:
    """Every doc type whose keywords appear in the text (word-bounded)."""
    t = (text or "").lower().replace("_", " ").replace("-", " ")
    found = []
    for code, meta in DOC_TYPES.items():
        for kw in meta["keywords"]:
            k = kw.replace("-", " ")
            if re.search(r"(?<![a-z])" + re.escape(k) + r"(?:s|es)?(?![a-z])", t):
                found.append(code)
                break
    return found
