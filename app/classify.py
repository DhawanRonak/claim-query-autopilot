"""Works out what a document is from its contents (not just its file name),
and whether it looks like it belongs to this patient."""
import re

from .llm import call_llm
from .taxonomy import DOC_TYPES, guess_doc_type

SYSTEM = """You classify Indian hospital / health-insurance claim documents.
Pick the single best type code from this list:
{codes}
Also read the patient/insured name printed on the document, if any.
Return ONLY JSON: {{"doc_type": "<code>", "confidence": 0.0-1.0, "name_on_document": "<name or empty>",
"readable": true|false, "note": "<under 12 words, e.g. 'blurry, amount unreadable'>"}}"""


def _codes():
    return "\n".join(f"  {k}: {v['label']}" for k, v in DOC_TYPES.items())


def _names_match(a: str, b: str) -> bool:
    ta = {t for t in re.findall(r"[a-z]+", (a or "").lower()) if len(t) > 2}
    tb = {t for t in re.findall(r"[a-z]+", (b or "").lower()) if len(t) > 2}
    return not ta or not tb or bool(ta & tb)


def classify(text: str, file_bytes: bytes, filename: str, patient_name: str) -> dict:
    """Returns {doc_type, confidence, classified_by, flag}."""
    out, engine = call_llm(SYSTEM.format(codes=_codes()), (text or "")[:6000], file_bytes, filename,
                           prefix=f"File name: {filename}\nExtracted text (may be empty for scans/photos):\n\n")
    flags = []
    if out and out.get("doc_type") in DOC_TYPES:
        dt, conf = out["doc_type"], float(out.get("confidence") or 0.6)
        name = out.get("name_on_document") or ""
        if name and not _names_match(name, patient_name):
            flags.append(f"Name on document is '{name}', check it belongs to this patient")
        if out.get("readable") is False:
            flags.append(f"Hard to read: {out.get('note') or 'image unclear'}")
        if conf < 0.6:
            flags.append("Low confidence on document type, please confirm")
        return {"doc_type": dt, "confidence": conf, "classified_by": engine, "flag": "; ".join(flags) or None}

    # no AI available: content keywords first, then the file name
    dt = guess_doc_type((text or "")[:300]) or guess_doc_type((text or "")[:3000])   # title area first
    by = "content keywords"
    if not dt:
        dt, by = guess_doc_type(filename), "file name"
    if not dt:
        return {"doc_type": "past_medical_records", "confidence": 0.2, "classified_by": "default",
                "flag": "Type not recognised, please set it"}
    return {"doc_type": dt, "confidence": 0.7 if by == "content keywords" else 0.5,
            "classified_by": by, "flag": None}
