"""Creates mock patients, locker documents, logins and sample query letters.

  python seed.py            # fresh demo data (wipes data/)
Replace with care.fi's real sample data using import_samples.py when you get it.
"""
import os
import shutil
import sys
import uuid
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

sys.path.insert(0, str(Path(__file__).parent))
from app import db  # noqa: E402
from app.taxonomy import label  # noqa: E402

ss = getSampleStyleSheet()


def make_pdf(path: Path, title: str, lines: list[str]):
    doc = SimpleDocTemplate(str(path), pagesize=A4, topMargin=50, bottomMargin=50)
    story = [Paragraph(title, ss["Title"]), Spacer(1, 10)]
    for l in lines:
        story.append(Paragraph(l.replace("&", "&amp;"), ss["Normal"]))
        story.append(Spacer(1, 6))
    doc.build(story)


C = ["kyc_aadhaar", "policy_copy", "discharge_summary", "final_bill", "investigation_reports"]
PATIENTS = [
    # id, name, insurer, hospital, diagnosis, agent, docs in locker
    ("CF-P001", "Ravi Kumar Sharma", "Star Health", "Apollo Hospitals, Bengaluru", "Acute appendicitis", "agent.priya",
     C + ["cancelled_cheque", "pharmacy_bills"]),
    ("CF-P002", "Ananya Iyer", "HDFC ERGO", "Manipal Hospital, Bengaluru", "Dengue fever", "agent.priya",
     ["kyc_pan", "health_card", "discharge_summary", "final_bill"]),
    ("CF-P003", "Mohammed Faizal", "ICICI Lombard", "Fortis Hospital, Mumbai", "Coronary artery disease (PTCA)", "agent.priya",
     C + ["consultation_papers", "implant_invoice"]),
    ("CF-P004", "Sunita Devi", "Niva Bupa", "Max Hospital, Delhi", "Cataract (right eye)", "agent.priya",
     ["kyc_aadhaar", "policy_copy", "discharge_summary"]),
    ("CF-P005", "Arjun Mehta", "Care Health", "Kokilaben Hospital, Mumbai", "Road traffic accident: femur fracture", "agent.priya",
     C + ["ot_notes"]),
    ("CF-P006", "Lakshmi Narayanan", "Star Health", "MIOT Hospital, Chennai", "Total knee replacement", "agent.rahul",
     C + ["cancelled_cheque", "previous_policy_copies", "past_medical_records"]),
    ("CF-P007", "Priya Banerjee", "Aditya Birla Health", "AMRI Hospital, Kolkata", "Gall stones (lap cholecystectomy)", "agent.rahul",
     ["policy_copy", "discharge_summary", "final_bill", "investigation_reports", "consultation_papers"]),
    ("CF-P008", "Harpreet Singh", "Bajaj Allianz", "Medanta, Gurugram", "Type 2 diabetes with ketoacidosis", "agent.rahul",
     C + ["kyc_pan", "cancelled_cheque", "claim_form"]),
    ("CF-P009", "Neha Kulkarni", "Tata AIG", "Ruby Hall Clinic, Pune", "Normal delivery", "agent.rahul",
     ["kyc_aadhaar", "health_card", "employer_certificate", "discharge_summary", "final_bill"]),
    ("CF-P010", "Vikram Reddy", "New India Assurance", "Yashoda Hospital, Hyderabad", "Pneumonia", "agent.rahul",
     C + ["cancelled_cheque", "pharmacy_bills", "consultation_papers"]),
]

QUERIES = {
    "CF-P001_star_query.pdf": ("Star Health and Allied Insurance - Claim Query", [
        "Claim No: STAR/2026/118842 | Patient: Ravi Kumar Sharma",
        "Dear Sir/Madam, with reference to the above claim, the following documents/clarifications are required:",
        "1. Complete discharge summary with signature of the treating doctor.",
        "2. Ultrasound abdomen report confirming the diagnosis.",
        "3. First consultation paper / prescription prior to admission.",
        "4. Cancelled cheque with the insured's name printed.",
        "5. Past treatment records, if any, for abdominal pain in the last 2 years.",
        "Kindly submit the above within 7 days failing which the claim will be closed.",
        "Regards, Claims Department"]),
    "CF-P002_hdfc_query.pdf": ("HDFC ERGO General Insurance - Deficiency Letter", [
        "Claim reference: HE-CL-552019 | Insured: Ananya Iyer",
        "We have reviewed the documents submitted. To proceed, please provide:",
        "a) NS1 antigen / dengue serology lab report and platelet count reports.",
        "b) Pharmacy bills along with prescriptions for medicines purchased outside the hospital.",
        "c) Aadhaar card copy of the insured for KYC.",
        "d) Copy of the policy document.",
        "e) Cancelled cheque or bank passbook front page for NEFT.",
        "Please submit within 15 days.", "Thanking you, Claims Team"]),
    "CF-P003_icici_query.pdf": ("ICICI Lombard - Additional Information Required", [
        "Claim No: IL/HC/2026/77310 | Patient: Mohammed Faizal",
        "1. Stent invoice and sticker of the implant used during PTCA.",
        "2. Coronary angiography report and CD.",
        "3. Indoor case papers with nursing notes for the entire stay.",
        "4. Past medical records for hypertension / diabetes, if any, with duration.",
        "5. The PAN card copy submitted earlier is illegible; please resubmit a clear copy.",
        "Regards"]),
    "CF-P004_niva_query.pdf": ("Niva Bupa - Query", [
        "Patient: Sunita Devi | Claim: NB-88213",
        "Further to our review, kindly provide the final hospital bill with itemised breakup, "
        "the IOL lens invoice with sticker, and the pre-operative biometry / A-scan report. "
        "Please also clarify the reason for delay in intimation of the claim beyond 48 hours of admission. "
        "Please submit within 7 days."]),
    "CF-P005_care_query.pdf": ("Care Health Insurance - Claim Query", [
        "Claim No: CH-2026-40021 | Insured: Arjun Mehta",
        "1. FIR / MLC copy for the road traffic accident.",
        "2. Please confirm whether the insured was under the influence of alcohol at the time of the accident (blood alcohol report if done).",
        "3. Operation notes for ORIF femur.",
        "4. X-ray right femur pre and post operative.",
        "Please submit within 10 days."]),
    "CF-P006_star_query.pdf": ("Star Health - Query Letter", [
        "Insured: Lakshmi Narayanan | Claim: STAR/2026/220117",
        "1. Previous year policy copies to establish continuity of cover (portability).",
        "2. Past treatment records for knee osteoarthritis including first consultation.",
        "3. Implant invoice and sticker for the knee prosthesis.",
        "4. Treating doctor's certificate on the duration of the ailment.",
        "Kindly submit within 7 days."]),
    "CF-P008_bajaj_query.pdf": ("Bajaj Allianz - Requirement Letter", [
        "Claim: BA/HL/2026/909121 | Patient: Harpreet Singh",
        "1. Duly signed claim form - the earlier form is unsigned.",
        "2. HbA1c report and previous diabetes treatment records with duration of diabetes.",
        "3. Self declaration on smoking and alcohol habits.",
        "4. Indoor case papers.",
        "Please submit within 7 days."]),
    "CF-P009_tata_query.pdf": ("Tata AIG - Pending Documents", [
        "Insured: Neha Kulkarni | Claim: TAG-MAT-55120",
        "1. Employee ID card / HR letter confirming the insured is a member of the group policy.",
        "2. Antenatal records and ultrasound reports.",
        "3. PAN card of the employee.",
        "4. Cancelled cheque.",
        "Please submit within 15 days."]),
}


def main():
    if "--if-empty" in sys.argv and db.DB_PATH.exists():
        print("Data already present, skipping seed.")
        return
    if db.DATA_DIR.exists():
        shutil.rmtree(db.DATA_DIR)
    db.init()
    docs_src = Path(__file__).parent / "data" / "_src"
    docs_src.mkdir(parents=True, exist_ok=True)

    pw = {"admin": os.getenv("SEED_ADMIN_PW", "CareFi@2026"),
          "agent": os.getenv("SEED_AGENT_PW", "Agent@2026"),
          "patient": os.getenv("SEED_PATIENT_PW", "Patient@2026")}
    db.ex("INSERT INTO users(username,password_hash,role,display_name) VALUES (?,?,?,?)",
          ("admin", db.hash_pw(pw["admin"]), "admin", "Ops Admin"))
    for a, n in (("agent.priya", "Priya (Agent)"), ("agent.rahul", "Rahul (Agent)")):
        db.ex("INSERT INTO users(username,password_hash,role,display_name) VALUES (?,?,?,?)",
              (a, db.hash_pw(pw["agent"]), "agent", n))

    for i, (pid, name, ins, hosp, diag, agent, docs) in enumerate(PATIENTS, 1):
        db.ex("""INSERT INTO patients(id,name,phone,email,insurer,policy_no,claim_no,hospital,admission_date,
                 diagnosis,assigned_agent,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
              (pid, name, f"+9190000000{i:02d}", f"patient{i:02d}@example.com", ins,
               f"POL-{ins[:3].upper()}-{100000 + i * 37}", f"CLM-{2026}{i:04d}", hosp,
               f"2026-10-{10 + i:02d}", diag, agent, db.now()))
        db.ex("INSERT INTO users(username,password_hash,role,display_name,patient_id) VALUES (?,?,?,?,?)",
              (pid.lower().replace("cf-", ""), db.hash_pw(pw["patient"]), "patient", name, pid))
        folder = db.UPLOAD_DIR / pid
        folder.mkdir(parents=True, exist_ok=True)
        for code in docs:
            fname = f"{label(code).split(' (')[0].replace(' / ', '_').replace(' ', '_')}.pdf"
            path = folder / f"{uuid.uuid4().hex}.pdf"
            make_pdf(path, label(code), [f"Patient: {name} ({pid})", f"Hospital: {hosp}",
                                         f"Diagnosis: {diag}", "Mock document generated for the demo."])
            import hashlib
            db.ex("""INSERT INTO documents(patient_id,doc_type,filename,stored_path,uploaded_at,uploaded_by,
                     confidence,classified_by,source,sha256) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                  (pid, code, fname, str(path), db.now(), "seed", 1.0, "sample data", "import",
                   hashlib.sha256(path.read_bytes()).hexdigest()))

    out = Path(__file__).parent / "sample_queries"
    out.mkdir(exist_ok=True)
    for fn, (title, lines) in QUERIES.items():
        make_pdf(out / fn, title, lines)
    shutil.rmtree(docs_src, ignore_errors=True)
    print("Seeded 10 patients, documents and logins.")
    print(f"  admin / {pw['admin']}   agent.priya, agent.rahul / {pw['agent']}   p001..p010 / {pw['patient']}")
    print(f"  Sample query letters in {out}")


if __name__ == "__main__":
    main()
