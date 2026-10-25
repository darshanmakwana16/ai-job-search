#!/usr/bin/env python3
"""Build the job dashboard: scored postings with one-click tailoring and referral helpers.

Reads a master resume (JSON, personal, gitignored) and one or more job files
(JSON lists of postings gathered by /scrape, the Indeed connector or a portal
CLI), scores every posting against the profile, and writes one self-contained
HTML page (personal output, gitignored under reports/).

Per posting the page offers:
  * a tailored resume - the master resume's own facts, reordered and
    re-emphasised for the posting's keywords (never new facts), downloadable
    as PDF / DOCX / TXT, with an optional "rewrite with Claude" pass when the
    page runs as a claude.ai artifact;
  * referral search links - recruiters, the likely hiring team, and alumni of
    the candidate's school at that company (LinkedIn people search plus a
    Google X-ray query);
  * two referral messages - <=220 characters for a message after connecting,
    <=200 for a LinkedIn connection note (the free-account cap).

Every posting description passes tools/posting_guard.py first: hidden and
invisible characters are stripped, and instruction-shaped lines are flagged
on the card instead of being trusted.

Job file format - a JSON list of objects:
  {"id", "title", "company", "location", "url", "source",
   "date" (YYYY-MM-DD, optional), "description" (optional),
   "companyUrl" (optional LinkedIn company URL), "salary" (optional),
   "seniority" (optional), "employmentType" (optional)}

Usage:
  python3 tools/build_job_dashboard.py --profile documents/cv/master_resume.json \\
      --jobs job_scraper/dashboard_jobs.json [--jobs more.json ...] \\
      [--companies job_scraper/target_companies.json] \\
      [--min-score 35] [--out reports/job_dashboard.html]
"""

import argparse
import datetime as dt
import html
import json
import re
import sys
from pathlib import Path
from urllib.parse import quote, quote_plus

sys.path.insert(0, str(Path(__file__).resolve().parent))
from posting_guard import guard  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = Path(__file__).resolve().parent / "job_dashboard_template.html"
DATA_MARKER = "/*__DASHBOARD_DATA__*/null"
CONNECT_NOTE_LIMIT = 200   # LinkedIn free-account connection note cap
MESSAGE_LIMIT = 220        # message after the connection is accepted
JD_EXCERPT = 5000

# Keyword lexicon: id -> (label, patterns, weight). A term counts as
# "supported" when one of its patterns matches the master resume itself, so the
# lexicon stays profile-agnostic. Weight 2 marks terms a screener reads as core.
LEXICON = {
    "iatf": ("IATF 16949", [r"\biatf\b", r"\bts\s?16949"], 2),
    "iso9001": ("ISO 9001", [r"iso\s?9001"], 2),
    "aiag": ("AIAG Core Tools", [r"\baiag\b", r"core tools"], 2),
    "apqp": ("APQP", [r"\bapqp\b"], 2),
    "ppap": ("PPAP", [r"\bppap\b", r"production part approval"], 2),
    "fmea": ("PFMEA / DFMEA", [r"\b[pd]?fmeas?\b", r"failure mode"], 2),
    "msa": ("MSA", [r"\bmsas?\b", r"measurement system"], 1),
    "grr": ("Gauge R&R", [r"ga(u)?ge r\s?&\s?r", r"\bgr\s?&\s?r\b", r"\bgrr\b"], 1),
    "spc": ("SPC", [r"\bspc\b", r"statistical process control", r"control charts?"], 2),
    "cpk": ("Cp/Cpk capability", [r"\bcpk\b", r"\bppk\b", r"process capability", r"capability stud"], 1),
    "controlplan": ("Control plans", [r"control plans?"], 1),
    "csr": ("Customer-specific requirements", [r"customer[- ]specific requirements", r"\bcsrs?\b"], 1),
    "qms": ("QMS / quality system", [r"\bqms\b", r"quality management system", r"quality systems?"], 1),
    "audit": ("Internal / process audits", [r"internal audit", r"process audit", r"layered (process )?audit", r"\baudits?\b"], 1),
    "8d": ("8D", [r"\b8d\b", r"8 disciplines"], 1),
    "5why": ("5-Why", [r"5[- ]?whys?", r"five whys?"], 1),
    "fishbone": ("Fishbone / Ishikawa", [r"fishbone", r"ishikawa"], 1),
    "pareto": ("Pareto analysis", [r"pareto"], 1),
    "rca": ("Root cause analysis", [r"root[- ]cause"], 2),
    "capa": ("CAPA / corrective action", [r"\bcapa\b", r"corrective (and preventive )?actions?", r"preventive actions?"], 2),
    "ncr": ("Non-conformance (NCR)", [r"\bncrs?\b", r"non[- ]?conform"], 1),
    "mrb": ("MRB disposition", [r"\bmrb\b", r"material review board"], 1),
    "ppm": ("PPM / defect trends", [r"\bppm\b", r"defect trends?", r"defect rates?"], 1),
    "gdt": ("GD&T", [r"gd\s?&\s?t", r"geometric dimensioning", r"y14\.5"], 2),
    "blueprint": ("Blueprint / drawing reading", [r"blueprints?", r"(read|reading|interpret)\w* (engineering |technical |cad |part )?drawings", r"2d/3d drawings"], 1),
    "inspection": ("Inspection", [r"inspect"], 1),
    "fai": ("FAI / FAIR", [r"\bfai\b", r"first article"], 1),
    "cmm": ("CMM", [r"\bcmm\b", r"coordinate measuring"], 1),
    "metrology": ("Metrology / gauges", [r"metrology", r"calipers?", r"micrometers?", r"measuring (instruments|equipment)", r"\bga(u)?ges\b"], 1),
    "calibration": ("Calibration", [r"calibrat"], 1),
    "dmaic": ("Six Sigma / DMAIC", [r"dmaic", r"six sigma", r"green belt"], 2),
    "lean": ("Lean manufacturing", [r"\blean\b"], 2),
    "5s": ("5S", [r"\b5s\b"], 1),
    "kaizen": ("Kaizen", [r"kaizen"], 1),
    "pokayoke": ("Poka-Yoke / error proofing", [r"poka[- ]?yoke", r"error[- ]proof", r"mistake[- ]proof"], 1),
    "vsm": ("Value stream mapping", [r"value stream", r"\bvsm\b"], 1),
    "kpi": ("KPIs / metrics", [r"\bkpis?\b", r"key performance", r"\bmetrics\b", r"\bmeasurables\b"], 1),
    "scrap": ("Scrap / COPQ reduction", [r"\bcopq\b", r"cost of (poor )?quality", r"\bscrap\b", r"\brework\b"], 1),
    "ci": ("Continuous improvement", [r"continu(ous|al) improvement", r"process improvement"], 2),
    "sop": ("SOPs / work instructions", [r"\bsops?\b", r"standard operating", r"work instructions?", r"standardi[sz]ed work"], 1),
    "fpy": ("First-pass yield", [r"first[- ]pass", r"\bfpy\b"], 1),
    "supplierq": ("Supplier quality", [r"supplier quality", r"\bscars?\b", r"supplier corrective", r"supplier (issues|development|performance)"], 1),
    "minitab": ("Minitab", [r"minitab"], 1),
    "excel": ("Excel", [r"\bexcel\b", r"pivot", r"power query", r"microsoft office", r"\bms office\b"], 1),
    "powerbi": ("Power BI", [r"power ?bi\b"], 1),
    "erp": ("SAP / ERP", [r"\bsap\b", r"\berp\b"], 2),
    "solidworks": ("SolidWorks", [r"solidworks"], 1),
    "cad": ("AutoCAD / CAD", [r"autocad", r"\bcad\b"], 1),
    "python": ("Python", [r"\bpython\b"], 1),
    "sql": ("SQL", [r"\bsql\b"], 1),
    "bom": ("Bills of materials", [r"\bboms?\b", r"bills? of materials?"], 1),
    "haccp": ("HACCP / food safety", [r"\bhaccp\b", r"food safety"], 1),
    "dataanalysis": ("Data analysis", [r"data analy", r"\banalytical\b", r"statistical analys"], 1),
    "forecast": ("Demand forecasting", [r"forecast"], 2),
    "inventory": ("Inventory management", [r"\binventor(y|ies)\b"], 2),
    "sourcing": ("Supplier selection / sourcing", [r"supplier (base|consolidation|selection|evaluation|approval)", r"vendor (selection|evaluation|management)", r"\bsourcing\b"], 2),
    "leadtime": ("Lead-time reduction", [r"lead[- ]?times?"], 1),
    "costsave": ("Cost reduction", [r"cost[- ](savings?|reductions?)", r"reduc\w* (part |manufacturing |operational )?costs?"], 1),
    "supplychain": ("Supply chain", [r"supply chain"], 2),
    "logistics": ("Logistics", [r"logistic"], 1),
    "vendor": ("Supplier / vendor coordination", [r"\bvendors?\b", r"\bsuppliers?\b"], 1),
    "projcoord": ("Project coordination", [r"project coordinat", r"coordinat\w* (multiple |the )?projects?", r"project management", r"manag\w* (multiple )?projects"], 2),
    "crossfunc": ("Cross-functional collaboration", [r"cross[- ]functional", r"collaborat"], 1),
    "leadership": ("Team leadership", [r"lead(ing)? (an? )?[\w–-]*\s?(person )?team", r"\bsupervis", r"leadership", r"\bmentor"], 1),
    # Usually gaps - listed so the page can say so honestly.
    "traceability": ("Traceability", [r"traceab"], 1),
    "po": ("Purchase orders / RFQs", [r"purchase orders?", r"\brfqs?\b", r"requests? for quot"], 2),
    "mrp": ("MRP", [r"\bmrp\b", r"material requirements? planning"], 1),
    "apics": ("APICS / CPIM / CSCP", [r"\bapics\b", r"\bcpim\b", r"\bcscp\b", r"\bascm\b"], 1),
    "scheduling": ("Production scheduling / planning", [r"production schedul", r"production planning", r"master schedul", r"capacity planning", r"work orders?"], 2),
    "shipping": ("Shipping / customs docs", [r"bills? of lading", r"\bcustoms\b", r"incoterms", r"\bfreight\b", r"commercial invoices?"], 1),
    "warehouse": ("Warehouse / 3PL", [r"warehous", r"\b3pl\b"], 1),
    "cyclecount": ("Cycle counts", [r"cycle counts?", r"physical (inventory|counts?)"], 1),
    "erp_other": ("Dynamics / Oracle / other ERP", [r"dynamics 365", r"\bd365\b", r"\boracle\b", r"netsuite", r"syacc", r"\bepicor\b", r"\bplex\b"], 1),
    "negotiation": ("Negotiation", [r"negotiat"], 1),
    "milestones": ("Schedules / milestones", [r"milestones?", r"timelines?", r"gantt"], 1),
    "stakeholder": ("Stakeholder / client communication", [r"stakeholders?", r"\bclients?\b"], 1),
    "pmp": ("PMP / CAPM", [r"\bpmp\b", r"\bcapm\b"], 1),
    "msproject": ("MS Project / Primavera", [r"ms project", r"microsoft project", r"primavera", r"\bp6\b"], 1),
    "budget": ("Budget / cost control", [r"budget", r"cost control"], 1),
    "risk": ("Risk management", [r"\brisks?\b"], 1),
    "changemgmt": ("Change management", [r"change management"], 1),
    "launch": ("Program launch / NPI", [r"\blaunch", r"\bnpi\b", r"new product introduction"], 1),
    "tpm": ("TPM / SMED", [r"\btpm\b", r"\bsmed\b"], 1),
    "automation": ("Automation / PLC / robotics", [r"\bplcs?\b", r"robot", r"automation"], 1),
    "automotive": ("Automotive / OEM supplier", [r"automotive", r"\boems?\b", r"tier ?[12]\b"], 1),
    "aerospace": ("Aerospace", [r"aerospace", r"aircraft", r"\buav\b", r"airframe"], 1),
    "as9100": ("AS9100", [r"as\s?9100"], 1),
    "ev": ("EV / battery", [r"\bbatter(y|ies)\b", r"electric vehicle", r"\bev\b"], 1),
    "injection": ("Injection molding", [r"injection mol"], 1),
    "welding": ("Welding / stamping", [r"\bwelding\b", r"\bstamping\b", r"\bcwb\b", r"\bcwi\b"], 1),
    "oilgas": ("Oil & gas / offshore", [r"oil (and|&) gas", r"offshore", r"subsea"], 1),
    "medical": ("Medical device / GMP", [r"iso\s?13485", r"medical device", r"\bgmp\b"], 1),
    "iso14001": ("ISO 14001", [r"iso\s?14001"], 1),
    "ndt": ("NDT", [r"\bndt\b", r"non[- ]destructive"], 1),
    "validation": ("Validation (IQ/OQ/PQ)", [r"\biq\s?/?\s?oq", r"\bpq\b", r"process validation"], 1),
    "asq": ("ASQ CQE / Black Belt", [r"\bcqe\b", r"\basq\b", r"black belt", r"\bcssbb\b"], 1),
}

# Requirements worth a warning on the card - never an automatic rejection.
FLAGS = [
    ("licence", "P.Eng / EIT licence mentioned", [r"\bp\.?\s?eng\b", r"\beit\b", r"engineer[- ]in[- ]training", r"professional engineers ontario", r"\bpeo\b"]),
    ("french", "French / bilingual mentioned", [r"bilingual", r"\bfrench\b", r"français"]),
    ("driver", "Driver's licence required", [r"driver'?s'? licen[cs]e", r"valid (ontario |class g )?licen[cs]e"]),
    ("clearance", "Security / Controlled Goods clearance", [r"security clearance", r"reliability (status|clearance)", r"controlled goods", r"secret clearance"]),
    ("us", "US travel / entry may be required", [r"access/entry to the united states", r"entry (in)?to the (u\.?s\.?|united states)", r"travel to the (u\.?s\.?|united states)", r"cross[- ]border travel", r"fast card", r"passport"]),
    ("citizen", "Citizenship / PR requirement mentioned", [r"canadian citizen", r"permanent resident", r"\bitar\b"]),
    ("shift", "Shift / weekend work", [r"\bshifts?\b", r"weekends?", r"overtime"]),
]

FAMILIES = {
    "QE": {
        "core": [r"quality (assurance |control )?engineer", r"supplier quality", r"\bqa/?qc\b", r"\bqc engineer", r"quality (coordinator|specialist|analyst|auditor|technologist|assurance (coordinator|specialist|analyst))",
                 r"continuous improvement", r"process (improvement )?engineer", r"operational excellence", r"\blean\b", r"six sigma", r"quality systems"],
        "adjacent": [r"manufacturing engineer", r"industrial engineer", r"quality (supervisor|lead|manager)", r"\blaunch", r"\bapqp\b", r"production engineer", r"reliability", r"quality", r"\bqa\b", r"\bqc\b"],
        "headlines": [(r"supplier quality", "Supplier Quality Engineer"), (r"continuous improvement|operational excellence|lean|six sigma", "Continuous Improvement Engineer"),
                      (r"process (improvement )?engineer", "Process Engineer"), (r"manufacturing engineer", "Manufacturing / Quality Engineer"),
                      (r"industrial engineer", "Industrial Engineer"), (r"qa/?qc|qc engineer", "QA/QC Engineer"), (r"launch", "Launch / Quality Engineer"),
                      (r"quality (coordinator|specialist|analyst|technologist|auditor|assurance)", "Quality Assurance Specialist"), (r"quality", "Quality Engineer")],
    },
    "PM": {
        "core": [r"project coordinator", r"project administrator", r"(junior|assistant|associate) project manager", r"project manager assistant", r"program coordinator", r"project (engineer|analyst)", r"project controls?"],
        "adjacent": [r"project manager", r"program manager", r"planning coordinator", r"operations coordinator", r"production coordinator", r"project"],
        "headlines": [(r"project engineer", "Project Engineer"), (r"program coordinator", "Program Coordinator"), (r"project (analyst|controls?)", "Project Analyst"),
                      (r"project", "Project Coordinator"), (r"operations coordinator", "Operations Coordinator"), (r"production coordinator", "Production Coordinator")],
    },
    "SCM": {
        "core": [r"supply chain (coordinator|analyst|specialist|planner|associate)", r"procurement (coordinator|specialist|analyst|officer|agent)", r"purchasing (coordinator|agent|specialist|assistant)",
                 r"\bbuyer", r"(materials?|production|demand|supply) planner", r"inventory (analyst|control|coordinator|planner|specialist)", r"logistics (coordinator|analyst|specialist)",
                 r"materials? (coordinator|analyst|specialist)", r"\bsourcing\b"],
        "adjacent": [r"supply chain", r"logistics", r"procurement", r"purchasing", r"planning", r"planner", r"expedit", r"commodity", r"materials?\b"],
        "headlines": [(r"procurement|purchasing|buyer|sourcing", "Procurement & Supply Chain Coordinator"), (r"planner|planning", "Material & Production Planner"),
                      (r"inventory", "Inventory & Supply Chain Analyst"), (r"logistics", "Logistics & Supply Chain Coordinator"),
                      (r"analyst", "Supply Chain Analyst"), (r"supply chain|material", "Supply Chain Coordinator")],
    },
}

# Short, fact-based hooks for referral messages, keyed by lexicon id. Each one
# paraphrases a line of the master resume; ordered by how much they sell.
HOOK_PRIORITY = ["ppap", "apqp", "iatf", "aiag", "fmea", "spc", "cpk", "rca", "capa", "8d", "5why", "gdt", "inspection", "iso9001", "qms", "sop", "audit", "calibration",
                 "dmaic", "lean", "ci", "kaizen", "5s", "vsm", "forecast", "inventory", "sourcing", "costsave", "leadtime", "supplychain", "vendor", "projcoord", "crossfunc",
                 "leadership", "erp", "excel", "powerbi", "minitab", "haccp"]

# Software-testing postings share the "QA engineer" title with manufacturing
# quality; two or more of these with no industrial context marks one off-target.
SOFTWARE_QA = re.compile(r"selenium|cypress|playwright|test automation|automated tests?|test cases|api testing|unit tests?|qa automation|software (quality|testing|development|engineer)|\bsdlc\b|\bscrum\b|\bjira\b|ci/cd|devops|regression test|manual testing|mobile app|web applications?|\bjava(script)?\b|\bsaas\b", re.I)
INDUSTRIAL = re.compile(r"manufactur|\bproduction\b|\bplants?\b|factory|industrial|automotive|aerospace|assembly|machin|fabricat|supply chain|logistic|procure|purchas|inventor|warehous|supplier|mining|oil|energy|construction|food processing|packaging|materials", re.I)
FRENCH = re.compile(r"\b(nous|vous|avec|pour|des|les|une|dans|est|sont|votre|qualit\u00e9|exp\u00e9rience)\b", re.I)
AGENCY = re.compile(r"staffing|recruit|personnel|placement|talent|workforce|employment|search|npaworldwide|randstad|adecco|robert half|kelly services|manpower|\bhays\b|aerotek|airswift|insight global|\bteksystems\b|kforce|jan kelley", re.I)

STOPWORDS_COMPANY = re.compile(r",?\s*\b(inc|incorporated|ltd|limited|corp|corporation|co|llc|ulc|lp|plc)\b\.?|\((ca|canada)\)", re.I)


def norm_company(name: str) -> str:
    cleaned = STOPWORDS_COMPANY.sub("", name or "").strip(" ,.-")
    return re.sub(r"\s{2,}", " ", cleaned)


def short_title(title: str) -> str:
    t = re.split(r"\s+[-–—|]\s+|\s*\(|,\s", title or "")[0].strip()
    return t or title


# Keywords whose posting wording would overstate the resume ("mentor",
# "project management", "Microsoft Office") are written into a tailored resume
# under their lexicon label instead of the posting's own word.
LABEL_ONLY = {"leadership", "crossfunc", "vendor", "stakeholder", "dataanalysis", "projcoord", "inspection", "blueprint", "excel", "kpi", "scrap", "supplierq"}
# A posting's adjective gets the noun it modifies, so a skills line reads as a skill.
NOUNIFY = {"ncr": (r"conforming$", " product")}
# Domain words describe the employer, not a skill: scored, never written into a resume.
NO_ROW = {"automotive", "aerospace", "ev", "oilgas", "medical"}
# Industry acronyms and tool names the lexicon does not cover. Matched as whole
# words (all-caps entries case-sensitively), and only reported - never added to
# a resume unless the master resume already contains the same token.
EXTRA_TERMS = [
    "CNC", "OEE", "DFM", "DFA", "DOE", "ANOVA", "PDCA", "RCCA", "RPN", "CTQ", "SIPOC", "DPMO", "LPA", "CQI-9", "VDA", "IMDS", "PSW", "ISIR",
    "Kanban", "Takt", "Andon", "WIP", "JIT", "TPS", "MES", "PLM", "ECN", "ECO", "CMMS", "SCADA", "HMI", "FEA", "CAM", "Catia", "NX",
    "S&OP", "SIOP", "IBP", "DDMRP", "VMI", "EOQ", "FIFO", "WMS", "TMS", "EDI", "MRO", "LTL", "FTL", "CUSMA", "USMCA", "Incoterms", "CBSA",
    "3PL", "SCMP", "CPSM", "CSCMP", "JDE", "QAD", "Infor", "Kinaxis", "Coupa", "Ariba", "Tableau", "Smartsheet", "JMP", "VBA", "Power Automate",
    "PMBOK", "PRINCE2", "WBS", "EVM", "RACI", "CPM", "CAPEX", "OPEX", "Agile", "Asana", "Jira",
    "AS9102", "NADCAP", "IPC", "ESD", "RoHS", "ASTM", "API", "ITAR", "CWI", "N299", "N285", "N286", "SQF", "BRCGS", "FSSC", "GFSI", "CFIA",
    "ISO 17025", "ISO 45001", "CQA", "CMQ",
]


def surface_of(term: str, text: str, lex) -> str:
    """The posting's own wording for a lexicon term, widened to whole words."""
    for pat in lex[term][1]:
        m = pat.search(text)
        if m:
            start, end = m.start(), m.end()
            while start > 0 and text[start - 1].isalnum():
                start -= 1
            while end < len(text) and (text[end].isalnum() or text[end] in "+#"):
                end += 1
            return re.sub(r"\s+", " ", text[start:end]).strip()[:40]
    return lex[term][0]


def keyword_rows(text: str, desc: str, corpus: str, lex, matched, partial, gaps) -> list:
    """Every posting keyword with the posting's wording, its status against the
    resume, and what a tailored resume may write for it (only when backed)."""
    rows = []
    for kind, ids in (("ok", matched), ("tr", partial), ("gap", gaps)):
        for t in ids:
            surface = surface_of(t, text, lex)
            show = "" if t in NO_ROW else lex[t][0] if t in LABEL_ONLY else (surface[:1].upper() + surface[1:] if surface.islower() else surface)
            if t in NOUNIFY and re.search(NOUNIFY[t][0], show, re.I):
                show += NOUNIFY[t][1]
            rows.append({"t": t, "s": surface, "show": show if kind == "ok" else "", "k": kind, "w": lex[t][2], "label": lex[t][0]})
    taken = [m.span() for _, pats, _ in lex.values() for p in pats for m in p.finditer(desc)]
    for term in EXTRA_TERMS:
        flags = 0 if term.isupper() or any(c.isdigit() for c in term) else re.I
        pat = re.compile(r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])", flags)
        m = pat.search(desc)
        if not m or any(a < m.end() and m.start() < b for a, b in taken):
            continue
        backed = pat.search(corpus) is not None
        rows.append({"t": "x:" + term, "s": m.group(0), "show": term if backed else "", "k": "xok" if backed else "xgap", "w": 1, "label": term})
    return rows


def compile_lexicon():
    return {k: (label, [re.compile(p, re.I) for p in pats], w) for k, (label, pats, w) in LEXICON.items()}


def terms_in(text: str, lex) -> list:
    return [k for k, (_, pats, _) in lex.items() if any(p.search(text) for p in pats)]


def profile_corpus(profile: dict) -> str:
    parts = []
    for track in profile.get("tracks", {}).values():
        parts += [track.get("summary", ""), " ".join(track.get("tagline", []))]
    for group in profile.get("skills", []):
        parts += group.get("items", [])
    for section in ("experience", "projects", "additional"):
        for entry in profile.get(section, []):
            parts += [entry.get("title", "")] + entry.get("bullets", [])
    parts += profile.get("certifications", [])
    return "\n".join(parts)


def bullet_tags(profile: dict, lex) -> None:
    """Annotate every resume bullet and skill item with the lexicon ids it evidences."""
    for section in ("experience", "projects", "additional"):
        for entry in profile.get(section, []):
            entry["bulletTags"] = [terms_in(b, lex) for b in entry.get("bullets", [])]
    for group in profile.get("skills", []):
        group["itemTags"] = [terms_in(i, lex) for i in group.get("items", [])]


def classify_region(location: str, regions: list, other: str = "Elsewhere") -> dict:
    loc = (location or "").lower()
    for region in regions:
        if any(m in loc for m in region.get("match", [])):
            return {"name": region["name"], "bonus": region.get("bonus", 0)}
    return {"name": other, "bonus": 3}


def family_for(title: str) -> tuple:
    tl = title.lower()
    best = ("", 0, "")
    for fam, spec in FAMILIES.items():
        if any(re.search(p, tl) for p in spec["core"]):
            score = 35
        elif any(re.search(p, tl) for p in spec["adjacent"]):
            score = 22
        else:
            continue
        if score > best[1]:
            headline = next((h for p, h in spec["headlines"] if re.search(p, tl)), "")
            best = (fam, score, headline)
    return best


def years_required(text: str):
    found = []
    for m in re.finditer(r"(\d{1,2})\s*(?:\+|plus)?\s*(?:(?:-|–|to)\s*(\d{1,2}))?\s*\+?\s*(?:years?|yrs?)\b", text, re.I):
        lo = int(m.group(1))
        window = text[m.end(): m.end() + 80].lower()
        if 0 < lo <= 20 and ("experience" in window or "experience" in text[max(0, m.start() - 60): m.start()].lower()):
            found.append(lo)
    return min(found) if found else None


def experience_points(years, seniority: str) -> int:
    pts = 12 if years is None else 15 if years <= 3 else 10 if years == 4 else 6 if years == 5 else 2 if years <= 7 else 0
    s = (seniority or "").lower()
    if "director" in s or "executive" in s:
        pts -= 15
    elif "mid-senior" in s:
        pts -= 3
    return pts


def build_messages(job: dict, hooks: list, profile: dict) -> dict:
    ref = profile.get("referral", {})
    intro = ref.get("intro", "recent graduate")
    company = norm_company(job["company"]) or job["company"]
    title = short_title(job["title"])

    def fit(templates, limit):
        for t in templates:
            if len(t) <= limit:
                return t
        return templates[-1][:limit]

    h = hooks + ["Lean Six Sigma Green Belt"] * 2
    long_forms = [
        f"Hi [Name], I'm a {intro} applying for the {title} role at {company}. I bring {h[0]} and {h[1]}. Would you be open to referring me or sharing advice? Thank you!",
        f"Hi [Name], I'm a {intro} applying for {company}'s {title} role. I bring {h[0]} and {h[1]}. Open to referring me or sharing advice? Thanks!",
        f"Hi [Name], {intro} applying for {company}'s {title} role. I bring {h[0]} and {h[1]}. Open to a referral or advice? Thanks!",
        f"Hi [Name], I'm a {intro} applying for {company}'s {title} role, bringing {h[0]}. Would you be open to referring me or sharing advice? Thank you!",
        f"Hi [Name], {intro} here, applying for {company}'s {title} role ({h[0]}). Open to a referral or quick advice? Thanks!",
        f"Hi [Name], {intro} applying to {company}. I bring {h[0]}. Open to a referral or quick advice? Thanks!",
    ]
    short_forms = [
        f"Hi [Name], {intro} here, applying for {company}'s {title} role. I bring {h[0]}. Open to a referral or quick advice? Thanks!",
        f"Hi [Name], {intro} applying for {company}'s {title} role ({h[0]}). Open to a referral or advice? Thanks!",
        f"Hi [Name], {intro} applying to {company} ({h[0]}). Open to a referral or quick advice? Thanks!",
        f"Hi [Name], {intro} applying to {company}. Open to a referral or quick advice? Thanks!",
    ]
    return {"message": fit(long_forms, MESSAGE_LIMIT), "note": fit(short_forms, CONNECT_NOTE_LIMIT)}


def linkedin_people(keywords: str) -> str:
    return "https://www.linkedin.com/search/results/people/?keywords=" + quote(keywords) + "&origin=GLOBAL_SEARCH_HEADER"


def referral_links(job: dict, track: str, region: str, profile: dict) -> list:
    company = norm_company(job["company"]) or job["company"]
    team = {"QE": '("quality engineer" OR "quality manager" OR "supplier quality")',
            "PM": '("project manager" OR "project engineer" OR "operations manager")',
            "SCM": '("supply chain" OR purchasing OR "materials manager" OR planner)'}.get(track, "engineer")
    city = re.sub(r"^greater\s+|\s+metropolitan area$|\s+area$", "", (job.get("location") or "").split(",")[0].strip(), flags=re.I) or region
    links = []
    company_url = (job.get("companyUrl") or "").split("?")[0].rstrip("/")
    if company_url and "/company/" in company_url:
        company_url = re.sub(r"^https?://[a-z]{2,3}\.linkedin\.com", "https://www.linkedin.com", company_url)
        links.append({"kind": "recruiter", "label": "Recruiters (company page)", "url": company_url + "/people/?keywords=" + quote("recruiter OR talent")})
    links += [
        {"kind": "recruiter", "label": "Recruiters & HR", "url": linkedin_people(f'"{company}" (recruiter OR "talent acquisition" OR "human resources")')},
        {"kind": "team", "label": "Hiring team", "url": linkedin_people(f'"{company}" {team}')},
        {"kind": "alumni", "label": f"{profile.get('schoolShort', 'Alumni')} alumni", "url": linkedin_people(f'"{company}" "{profile.get("school", "")}"')},
        {"kind": "xray", "label": "Google X-ray", "url": "https://www.google.com/search?q=" + quote_plus(f'site:linkedin.com/in "{company}" (recruiter OR "talent acquisition" OR manager) "{city}"')},
    ]
    return links


def score_job(job: dict, profile: dict, lex, supported: set, corpus: str = "") -> dict:
    desc_raw = job.get("description") or ""
    desc, findings = guard(desc_raw, "text") if desc_raw else ("", {"instruction_like": [], "verdict": "posting_guard: no description"})
    text = f"{job['title']}\n{desc}"
    fam, fam_pts, headline = family_for(job["title"])
    jd_terms = terms_in(text, lex)
    transferable = profile.get("transferable", {})
    matched = [t for t in jd_terms if t in supported]
    partial = [t for t in jd_terms if t not in supported and t in transferable]
    gaps = [t for t in jd_terms if t not in supported and t not in transferable]
    weight = lambda ids: sum(lex[i][2] for i in ids)  # noqa: E731
    total = weight(jd_terms)
    if desc:
        kw_pts = round(35 * (weight(matched) + 0.5 * weight(partial)) / max(total, 14))
    else:
        kw_pts = 15
    if not fam:
        counts = {f: sum(1 for t in matched if t in {"QE": {"iatf", "apqp", "ppap", "spc", "rca", "capa", "inspection", "gdt"},
                                                       "PM": {"projcoord", "milestones", "stakeholder", "crossfunc"},
                                                       "SCM": {"supplychain", "inventory", "forecast", "sourcing", "logistics", "po"}}[f]) for f in FAMILIES}
        fam = max(counts, key=counts.get) if any(counts.values()) else "QE"
        fam_pts = 10
    years = years_required(desc)
    exp_pts = experience_points(years, job.get("seniority", ""))
    region = classify_region(job.get("location", ""), profile.get("regions", []), profile.get("otherRegion", "Elsewhere"))
    flags = [label for key, label, pats in FLAGS if any(re.search(p, text, re.I) for p in pats)]
    penalty = (8 if "P.Eng / EIT licence mentioned" in flags and re.search(r"(must|required|requirement)[^.]{0,60}p\.?\s?eng|p\.?\s?eng[^.]{0,40}(required|must)", text, re.I) else 0)
    penalty += 10 if re.search(r"(bilingual|french)[^.]{0,40}(required|essential|must)|(must|required)[^.]{0,40}(bilingual|french)", text, re.I) else 0
    software_hits = len(set(m.group(0).lower() for m in SOFTWARE_QA.finditer(text)))
    industrial_title = re.search(r"quality engineer|supplier|manufactur|process|plant|production", job["title"], re.I)
    off_target = (software_hits >= 2 and len(INDUSTRIAL.findall(text)) < 3) or (software_hits >= 3 and not industrial_title)
    if desc and not INDUSTRIAL.search(text):
        penalty += 15
        flags.append("No manufacturing / industrial context")
    if len(FRENCH.findall(desc)) >= 12:
        penalty += 15
        flags.append("Posting is in French - French likely required")
    if AGENCY.search(job.get("company", "")):
        flags.append("Recruiting agency - contact the recruiter, the employer is unnamed")
    score = max(0, min(100, fam_pts + kw_pts + exp_pts + region["bonus"] - penalty))
    hooks_src = profile.get("hooks", {})
    hooks = [hooks_src[t] for t in HOOK_PRIORITY if t in matched and t in hooks_src]
    hooks = list(dict.fromkeys(hooks))[:2]
    if not hooks:
        hooks = [hooks_src.get(t) for t in ("rca", "dmaic") if hooks_src.get(t)]
    return {
        "offTarget": off_target, "track": fam, "headline": headline or profile["tracks"].get(fam, {}).get("headline", ""),
        "score": score, "breakdown": {"title": fam_pts, "keywords": kw_pts, "experience": exp_pts, "location": region["bonus"], "penalty": -penalty},
        "matched": matched, "partial": partial, "gaps": gaps, "years": years, "region": region["name"], "flags": flags,
        "guard": {"verdict": findings.get("verdict", ""), "instructionLike": findings.get("instruction_like", [])[:3]},
        "jd": desc[:JD_EXCERPT], "hooks": hooks,
        "keywords": keyword_rows(text, desc, corpus, lex, matched, partial, gaps),
    }


def dedupe(jobs: list) -> list:
    seen = {}
    for job in jobs:
        key = (norm_company(job.get("company", "")).lower(), short_title(job.get("title", "")).lower(), (job.get("location") or "").split(",")[0].lower())
        if key in seen:
            keep = seen[key]
            keep.setdefault("alsoOn", []).append({"source": job.get("source", ""), "url": job.get("url", "")})
            if len(job.get("description") or "") > len(keep.get("description") or ""):
                keep["description"] = job["description"]
            continue
        seen[key] = job
    return list(seen.values())


def build(profile: dict, jobs: list, companies: list, min_score: int) -> dict:
    lex = compile_lexicon()
    corpus = profile_corpus(profile)
    supported = set(terms_in(corpus, lex))
    bullet_tags(profile, lex)
    out = []
    for job in dedupe(jobs):
        scored = score_job(job, profile, lex, supported, corpus)
        if scored["offTarget"] or scored["score"] < min_score:
            continue
        record = {k: job.get(k, "") for k in ("id", "title", "company", "companyUrl", "location", "date", "url", "source", "salary", "seniority", "employmentType")}
        record["alsoOn"] = job.get("alsoOn", [])
        record.update(scored)
        record["links"] = referral_links(job, scored["track"], scored["region"], profile)
        record.update(build_messages(job, scored["hooks"], profile))
        out.append(record)
    out.sort(key=lambda j: j.get("date") or "", reverse=True)
    out.sort(key=lambda j: j["score"], reverse=True)
    return {
        "generated": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "profile": profile,
        "lexicon": {k: {"label": v[0], "w": v[2]} for k, v in LEXICON.items()},
        "supported": sorted(supported),
        "jobs": out,
        "companies": companies,
        "otherRegion": profile.get("otherRegion", "Elsewhere"),
        "limits": {"message": MESSAGE_LIMIT, "note": CONNECT_NOTE_LIMIT},
    }


def render(data: dict, template: Path) -> str:
    page = template.read_text(encoding="utf-8")
    if DATA_MARKER not in page:
        raise SystemExit(f"build_job_dashboard: data marker missing from {template}")
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return page.replace(DATA_MARKER, payload, 1).replace("__PAGE_TITLE__", html.escape(data["profile"].get("dashboardTitle", "Job Radar")))


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(prog="build_job_dashboard.py", description="Build the job dashboard; see module docstring.")
    ap.add_argument("--profile", type=Path, default=ROOT / "documents" / "cv" / "master_resume.json")
    ap.add_argument("--jobs", type=Path, action="append", required=True, help="job list JSON (repeatable)")
    ap.add_argument("--companies", type=Path, default=None, help="optional target-employer list JSON")
    ap.add_argument("--min-score", type=int, default=35)
    ap.add_argument("--out", type=Path, default=ROOT / "reports" / "job_dashboard.html")
    ap.add_argument("--data-out", type=Path, default=None, help="also write the scored data as JSON")
    args = ap.parse_args(argv)
    profile = json.loads(args.profile.read_text(encoding="utf-8"))
    jobs = []
    for path in args.jobs:
        jobs += json.loads(path.read_text(encoding="utf-8"))
    companies = json.loads(args.companies.read_text(encoding="utf-8")) if args.companies else []
    data = build(profile, jobs, companies, args.min_score)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render(data, TEMPLATE), encoding="utf-8")
    if args.data_out:
        args.data_out.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    regions = {}
    for job in data["jobs"]:
        regions[job["region"]] = regions.get(job["region"], 0) + 1
    print(f"build_job_dashboard: {len(data['jobs'])} jobs (of {len(jobs)} read) -> {args.out}  {regions}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
