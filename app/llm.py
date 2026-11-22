"""Reads a query letter and returns the separate points the insurer raised.

Engines, picked automatically from .env (every one with a key is tried in this order, then rules):
  gemini    -> GEMINI_API_KEY     (free key from aistudio.google.com)
  xai       -> XAI_API_KEY        (Grok; text letters only)
  anthropic -> ANTHROPIC_API_KEY
  openai    -> OPENAI_API_KEY
  rules     -> no key needed; regex + keyword parser so the demo always runs

Gemini and Claude receive the PDF/image itself when the letter has no text
layer (scanned letters), so OCR comes for free.
"""
import logging
import base64
import json
import os
import re

import httpx

from .taxonomy import DOC_TYPES, all_doc_types_in, label

TIMEOUT = 60

WHY = {
    "discharge_summary": "the insurer needs to see the diagnosis and treatment given during your stay",
    "final_bill": "the insurer needs the final, itemised hospital charges to settle the amount",
    "pharmacy_bills": "the insurer needs proof of the medicines that were bought",
    "investigation_reports": "the insurer wants to see the test results that support the diagnosis",
    "consultation_papers": "the insurer wants to see the doctor's notes from before you were admitted",
    "past_medical_records": "the insurer wants to check how long you have had this condition",
    "policy_copy": "the insurer needs to confirm your policy details",
    "health_card": "the insurer needs your health card to confirm you are covered",
    "previous_policy_copies": "the insurer needs to confirm how long you have been continuously insured",
    "kyc_aadhaar": "the insurer needs to verify your identity",
    "kyc_pan": "the insurer needs to verify your identity",
    "cancelled_cheque": "the insurer needs your bank details to pay the claim amount",
    "claim_form": "the insurer needs the claim form signed by you",
    "fir_mlc": "for accident cases the insurer needs the police / medico-legal record",
    "employer_certificate": "your policy is through your employer, so the insurer needs proof of employment",
    "indoor_case_papers": "the insurer wants the day-by-day treatment record from the hospital",
    "ot_notes": "the insurer wants the surgeon's notes on the procedure",
    "implant_invoice": "the insurer needs the invoice for the implant used",
    "treating_doctor_certificate": "the insurer wants the treating doctor to confirm why the treatment was needed",
    "patient_declaration": "the insurer has asked for an explanation in your own words",
}

SYSTEM = """You are a health-insurance claims analyst at an Indian hospital revenue-cycle company.
You read insurer/TPA query letters and list every separate thing the insurer is asking for.

Rules:
- One point per distinct ask. If a numbered item asks for two unrelated things, split it.
- Ignore greetings, claim references, legal boilerplate and deadlines (deadline goes in "deadline").
- Map each point to one or more document codes from this list (use only these codes):
{codes}
- kind = "document" if a document can satisfy it, "clarification" if the insurer wants an explanation.
  A clarification that the patient must explain maps to patient_declaration; one only the doctor can
  answer maps to treating_doctor_certificate.
- fresh_copy_needed = true only if the letter says an earlier copy was illegible, incomplete, unsigned,
  not attested, or asks for an original/updated/corrected version.
- patient_reason: ONE short sentence in very simple English (class-8 level) explaining why it is needed. No jargon.
- Copy the insurer's ask faithfully but briefly into "text".

Return ONLY JSON:
{{"summary": "<one line>", "deadline": "<as written or empty>",
  "points": [{{"text": "...", "kind": "document|clarification", "doc_types": ["code"],
              "fresh_copy_needed": false, "patient_reason": "..."}}]}}"""


log = logging.getLogger("llm")


def _codes_block():
    return "\n".join(f"  {k}: {v['label']} (held by {v['source']})" for k, v in DOC_TYPES.items())


def engine_name() -> str:
    forced = os.getenv("LLM_ENGINE", "").strip().lower()
    if forced == "rules":
        return "rules"
    chain = engine_chain()
    return chain[0] if chain else "rules"


KEY_ENVS = (("GEMINI_API_KEY", "gemini"), ("XAI_API_KEY", "xai"), ("ANTHROPIC_API_KEY", "anthropic"),
            ("OPENAI_API_KEY", "openai"))


def engine_chain() -> list[str]:
    """Every AI engine that has a key, forced one first. Tried in order before the rules reader."""
    forced = os.getenv("LLM_ENGINE", "").strip().lower()
    names = [n for env, n in KEY_ENVS if os.getenv(env)]
    if forced and forced != "rules":
        names = [forced] + [n for n in names if n != forced]
    return names


def _parse_json(s: str) -> dict:
    s = s.strip()
    s = re.sub(r"^```(?:json)?|```$", "", s, flags=re.M).strip()
    start, end = s.find("{"), s.rfind("}")
    return json.loads(s[start:end + 1])


def _mime(filename: str) -> str:
    f = filename.lower()
    if f.endswith(".pdf"):
        return "application/pdf"
    if f.endswith(".png"):
        return "image/png"
    if f.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    return "text/plain"


# ---- engines -----------------------------------------------------------------
GEMINI_MODELS = ["gemini-flash-latest", "gemini-2.5-flash", "gemini-2.0-flash"]


def _gemini(text, file_bytes, filename, system=None, prefix="Query letter text follows (may be empty if scanned):\n\n"):
    system = system or SYSTEM.format(codes=_codes_block())
    parts = [{"text": prefix + (text or "")}]
    if file_bytes and _mime(filename) != "text/plain":
        parts.append({"inline_data": {"mime_type": _mime(filename),
                                      "data": base64.b64encode(file_bytes).decode()}})
    body = {"system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"temperature": 0, "response_mime_type": "application/json"}}
    import time
    from concurrent.futures import ThreadPoolExecutor, as_completed
    key = os.environ["GEMINI_API_KEY"]
    errors: list[str] = []

    def try_model(model):
        """One model, one shot. (parsed_json or None, http_status)"""
        try:
            r = httpx.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                           headers={"x-goog-api-key": key}, json=body, timeout=25)
        except httpx.TimeoutException:
            errors.append(f"{model}: timed out")
            return None, 0
        except Exception as e:
            errors.append(f"{model}: {type(e).__name__}")
            return None, 0
        if r.status_code == 200:
            try:
                return _parse_json(r.json()["candidates"][0]["content"]["parts"][0]["text"]), 200
            except Exception as e:
                errors.append(f"{model}: unreadable reply ({type(e).__name__})")
                return None, 200
        try:
            msg = r.json().get("error", {}).get("message", "")[:70]
        except Exception:
            msg = r.text[:70]
        errors.append(f"{model}: {r.status_code} {msg}")
        return None, r.status_code

    # Ask several models at once and take the first good answer: one overloaded or retired model can't stall a letter.
    cands = _gemini_candidates(key)
    ex = ThreadPoolExecutor(max_workers=len(cands))
    futs = [ex.submit(try_model, m) for m in cands]
    codes = []
    try:
        for f in as_completed(futs, timeout=30):
            out, code = f.result()
            codes.append(code)
            if out:
                return out
    except Exception:
        errors.append("no answer in 30s")
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    raise RuntimeError("Gemini " + "; ".join(e[:75] for e in errors)[:300])


_CAND_CACHE: dict = {"t": 0.0, "models": []}


def _gemini_models_for_key(key: str) -> list[str]:
    """Flash models this key can actually call, newest first (cached 10 min)."""
    import time
    if time.time() - _CAND_CACHE["t"] < 600 and _CAND_CACHE["models"]:
        return _CAND_CACHE["models"]
    try:
        r = httpx.get("https://generativelanguage.googleapis.com/v1beta/models?pageSize=200",
                      headers={"x-goog-api-key": key}, timeout=8)
        names = [m["name"].split("/", 1)[1] for m in r.json().get("models", [])
                 if "generateContent" in m.get("supportedGenerationMethods", [])]
    except Exception:
        return []
    ok = [n for n in names if "flash" in n and not any(x in n for x in ("image", "tts", "live", "audio", "thinking", "robotics", "embedding", "native"))]
    ok.sort(key=lambda n: ("lite" in n, "preview" in n or "exp" in n, [-int(x) for x in re.findall(r"\d+", n)[:2]]))
    _CAND_CACHE.update(t=time.time(), models=ok)
    return ok


def _gemini_candidates(key: str) -> list[str]:
    found = _gemini_models_for_key(key)
    order = [os.getenv("GEMINI_MODEL"), "gemini-flash-latest"] + found[:7] + GEMINI_MODELS
    return list(dict.fromkeys(m for m in order if m))[:9]


def gemini_diagnose() -> list[str]:
    """For Settings -> Test AI reader: which models answer, and how fast."""
    import time
    from concurrent.futures import ThreadPoolExecutor
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        return ["No GEMINI_API_KEY set."]
    cands = _gemini_candidates(key)
    body = {"contents": [{"role": "user", "parts": [{"text": 'Reply with exactly this JSON: {"ok": true}'}]}],
            "generationConfig": {"temperature": 0, "response_mime_type": "application/json"}}

    def one(m):
        t = time.time()
        try:
            r = httpx.post(f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent",
                           headers={"x-goog-api-key": key}, json=body, timeout=25)
            dt = f"{time.time() - t:.1f}s"
            if r.status_code == 200:
                return f"OK   {m}  {dt}"
            try:
                msg = r.json().get("error", {}).get("message", "")[:70]
            except Exception:
                msg = ""
            return f"FAIL {m}  {r.status_code} {msg}"
        except httpx.TimeoutException:
            return f"FAIL {m}  no answer in 25s"
        except Exception as e:
            return f"FAIL {m}  {type(e).__name__}"

    with ThreadPoolExecutor(max_workers=len(cands)) as ex:
        res = list(ex.map(one, cands))
    return res + [f"Models this key lists: {', '.join(_gemini_models_for_key(key)[:9]) or 'none found'}"]


def _anthropic(text, file_bytes, filename, system=None, prefix="Query letter text follows (may be empty if scanned):\n\n"):
    system = system or SYSTEM.format(codes=_codes_block())
    content = [{"type": "text", "text": prefix + (text or "")}]
    mime = _mime(filename)
    if file_bytes and mime == "application/pdf":
        content.insert(0, {"type": "document", "source": {"type": "base64", "media_type": mime,
                                                          "data": base64.b64encode(file_bytes).decode()}})
    elif file_bytes and mime.startswith("image/"):
        content.insert(0, {"type": "image", "source": {"type": "base64", "media_type": mime,
                                                       "data": base64.b64encode(file_bytes).decode()}})
    r = httpx.post("https://api.anthropic.com/v1/messages", timeout=TIMEOUT,
                   headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"],
                            "anthropic-version": "2023-06-01"},
                   json={"model": os.getenv("ANTHROPIC_MODEL", "claude-haiku-5-5"), "max_tokens": 2000,
                         "temperature": 0, "system": system,
                         "messages": [{"role": "user", "content": content}]})
    r.raise_for_status()
    return _parse_json(r.json()["content"][0]["text"])


def _openai(text, file_bytes, filename, system=None, prefix="Query letter text follows (may be empty if scanned):\n\n"):
    system = system or SYSTEM.format(codes=_codes_block())
    r = httpx.post("https://api.openai.com/v1/chat/completions", timeout=TIMEOUT,
                   headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
                   json={"model": os.getenv("OPENAI_MODEL", "gpt-4.1-mini"), "temperature": 0,
                         "response_format": {"type": "json_object"},
                         "messages": [{"role": "system", "content": system},
                                      {"role": "user", "content": prefix + (text or "")}]})
    r.raise_for_status()
    return _parse_json(r.json()["choices"][0]["message"]["content"])


def _xai(text, file_bytes, filename, system=None, prefix="Query letter text follows (may be empty if scanned):\n\n"):
    """Grok via xAI's OpenAI-compatible API. Text only: scanned letters need Gemini/Claude/OpenAI."""
    system = system or SYSTEM.format(codes=_codes_block())
    key = os.environ["XAI_API_KEY"]
    hdr = {"Authorization": f"Bearer {key}"}
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": prefix + (text or "")}]
    errors, tried = [], []

    def go(model):
        tried.append(model)
        r = httpx.post("https://api.x.ai/v1/chat/completions", headers=hdr, timeout=30,
                       json={"model": model, "temperature": 0, "messages": msgs})
        if r.status_code == 200:
            return _parse_json(r.json()["choices"][0]["message"]["content"])
        errors.append(f"{model}: {r.status_code} {r.text[:90]}")
        return None

    first = os.getenv("XAI_MODEL", "grok-4-fast-non-reasoning")
    out = go(first)
    if out:
        return out
    try:                                            # ask xAI which models this key can use
        ids = [m["id"] for m in httpx.get("https://api.x.ai/v1/models", headers=hdr, timeout=10).json().get("data", [])]
    except Exception:
        ids = []
    for mid in [i for i in ids if i.startswith("grok") and not any(x in i for x in ("image", "vision", "imagine", "video"))
                and i not in tried][:3]:
        out = go(mid)
        if out:
            return out
    raise RuntimeError("Grok " + "; ".join(errors)[:300])


# ---- no-key fallback -----------------------------------------------------------
MARKER = re.compile(r"^\s*(?:\(?\d{1,2}[\.\)]|\(?[a-hj-z][\.\)]|\(?[ivx]{1,4}[\.\)]|[•\-\*▪●►])\s+", re.I)
ASK = re.compile(r"\b(submit|provide|send|furnish|forward|share|clarify|explain|required|require|need|kindly|please|confirm|mention|state)\b", re.I)
FRESH = re.compile(r"\b(illegible|not legible|unclear|not clear|incomplete|unsigned|not signed|attested|original|updated|corrected|resubmit|re-submit|again)\b", re.I)
DEADLINE = re.compile(r"\s*(kindly|please)?\s*(submit|send|provide)?\s*(the above|these|the same)?\s*(documents\s*)?within\s+\d+\s+(working\s+)?(days|hours)[^.]*\.?\s*$", re.I)
CLARIFY = re.compile(r"\b(clarify|clarification|explain|explanation|reason|why|justify|justification|confirm)\b", re.I)


def _rules(text, *_):
    lines = [l.rstrip() for l in (text or "").splitlines()]
    chunks, cur = [], None
    for line in lines:
        if MARKER.match(line):
            if cur:
                chunks.append(cur)
            cur = MARKER.sub("", line).strip()
        elif cur is not None and line.strip():
            if re.match(r"^\s*(regards|thanking|yours|sincerely|note:|please note)", line, re.I):
                chunks.append(cur)
                cur = None
            else:
                cur += " " + line.strip()
        elif cur is not None and not line.strip():
            chunks.append(cur)
            cur = None
    if cur:
        chunks.append(cur)
    numbered = bool(chunks)
    if not chunks:  # unnumbered letter: fall back to request sentences
        keep = [l for l in (text or "").splitlines()[1:]   # drop title line
                if not re.match(r"^\s*(claim|patient|insured|policy|ref|date|to|subject)\b.*[:|]", l, re.I)]
        sents = re.split(r"(?<=[.;?])\s+", " ".join(keep))
        chunks = [re.sub(r"\s+", " ", s).strip() for s in sents if ASK.search(s)]

    # split list-style sentences ("the bill, the invoice and the report") into separate asks
    split_chunks = []
    for c in chunks:
        parts = [x.strip(" .") for x in re.split(r",\s*(?:and\s+)?|\s+and\s+(?=the\s)", c) if x.strip(" .")]
        while len(parts) > 2 and not all_doc_types_in(parts[0]):   # drop lead-in like "Further to our review"
            parts.pop(0)
        if len(parts) > 1 and not CLARIFY.search(c) and all(all_doc_types_in(x) for x in parts):
            split_chunks += parts
        else:
            split_chunks.append(c)
    chunks = split_chunks

    points = []
    for c in chunks:
        c = DEADLINE.sub("", c).strip()
        if len(c) < 6:
            continue
        codes = all_doc_types_in(c)
        if not codes and (not numbered and not ASK.search(c)):
            continue
        is_clar = bool(CLARIFY.search(c)) and (not codes or codes == ["patient_declaration"])
        if is_clar and not codes:
            codes = ["patient_declaration"]
        if len(codes) > 1 and "patient_declaration" in codes and not CLARIFY.search(c):
            codes.remove("patient_declaration")
        if CLARIFY.search(c) and "patient_declaration" in codes:
            is_clar = True
        points.append({
            "text": c[:400],
            "kind": "clarification" if is_clar else "document",
            "doc_types": codes,
            "fresh_copy_needed": bool(FRESH.search(c)),
            "patient_reason": WHY.get(codes[0], "the insurer needs this to process your claim") if codes else "",
        })
    m = re.search(r"within\s+(\d+\s+(?:days|working days|hours))", text or "", re.I)
    return {"summary": f"{len(points)} point(s) raised", "deadline": m.group(1) if m else "", "points": points}


ENGINES = {"gemini": _gemini, "xai": _xai, "anthropic": _anthropic, "openai": _openai, "rules": _rules}


def extract_points(text: str, file_bytes: bytes = b"", filename: str = "") -> tuple[dict, str]:
    """Returns (result, engine_used). Tries every configured AI in turn, then the rules reader."""
    out, name, errs = None, "rules", []
    for eng in engine_chain():
        try:
            out, name = ENGINES[eng](text, file_bytes, filename), eng
            break
        except Exception as e:  # never block the pipeline on an API hiccup
            errs.append(f"{eng}: {str(e)[:160] or type(e).__name__}")
    if out is None:
        out = _rules(text)
        name = f"rules (fallback: {' | '.join(errs)[:300]})" if errs else "rules"
    # sanitise: keep only known codes, fill missing reasons
    for p in out.get("points", []):
        p["doc_types"] = [c for c in p.get("doc_types", []) if c in DOC_TYPES]
        if not p.get("patient_reason") and p["doc_types"]:
            p["patient_reason"] = WHY.get(p["doc_types"][0], "")
        p.setdefault("kind", "document")
        p.setdefault("fresh_copy_needed", False)
    return out, name


def call_llm(system: str, text: str, file_bytes: bytes, filename: str, prefix: str) -> tuple[dict | None, str]:
    """Generic JSON call on the first configured engine that works. Returns (None, 'rules') with no key."""
    errs = []
    for eng in engine_chain():
        try:
            return ENGINES[eng](text, file_bytes, filename, system=system, prefix=prefix), eng
        except Exception as e:
            errs.append(f"{eng}: {str(e)[:160] or type(e).__name__}")
    return None, (f"rules (fallback: {' | '.join(errs)[:300]})" if errs else "rules")
