#!/usr/bin/env python3
"""
prepare_cases.py — build the anonymized case file the Content Ethics Advisor app reads.

Run this once on your own machine (never on Streamlit Cloud) whenever the case log
changes. It reads the ORIGINAL Japanese case-log workbook and uses Claude on Bedrock
to rewrite every case as a generic, anonymized English record: no titles, project
names, codenames, companies, departments, people, case numbers, dates, in-game
place/character names or asset IDs. The app only ever sees this rewritten file, so
it cannot leak what it never received.

Outputs (next to the input file unless -o is given):
  cases_sanitized.enc    encrypted case file — safe to commit to the app's repo
  cases_encryption_key.txt  the key; paste into the app's secrets, never commit
  cases_sanitized.json   plain version (for an internal server); never commit
  cases_review.csv       spot-check this before deploying (sanitized text only)

The key file is reused on later runs, so updating the cases doesn't require
changing the app's secrets.

Usage:
  python prepare_cases.py Confidential_RInri_consultation_cases.xlsx --region us-west-2
  python prepare_cases.py cases.xlsx --extra-terms terms.txt   # extra names to block
  python prepare_cases.py cases.xlsx --profile myprofile --workers 4

terms.txt: one term per line — in-game place names, character names, codenames, or
anything else that must never appear (e.g. a fictional district name). Names from the
company / department / project / asker / title columns are blocked automatically.

Progress is cached in <input>.sanitize_cache.json so an interrupted run resumes.
Requires: pip install boto3 openpyxl cryptography
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_MODEL = "us.anthropic.claude-opus-5-5"

# Column positions in the FY27 case sheet (0-based, as read by openpyxl values_only).
# Matched by the Japanese header first; these positions are the fallback.
COLUMNS = {
    "no":          ("No.", 1),
    "date":        ("発生日", 2),
    "company":     ("会社", 3),
    "inq_dept":    ("問い合わせ部門", 4),
    "dev_pj":      ("開発部門／PJ", 5),
    "asker":       ("質問者", 6),
    "title":       ("タイトル", 7),
    "channel":     ("経緯", 8),
    "category":    ("倫理カテゴリ", 9),
    "content":     ("内容", 10),
    "unit_reply":  ("表現倫理ユニットの対応", 11),
    "dept_reply":  ("問い合わせ部門/開発担当部門の対応", 12),
    "key_points":  ("判断のポイント", 14),
    "status":      ("対応状況", 15),
    "tags":        ("検索TAG", 18),
    "asia":        ("アジア", 19),
    "west":        ("欧米", 20),
    "both":        ("亜/欧米 両方", 21),
}
# Columns whose values are blocked from the output (they identify teams/titles/people).
IDENTIFYING = ["company", "inq_dept", "dev_pj", "asker", "title"]

VERDICTS = ["No issue", "Acceptable in context", "Change recommended", "Change required",
            "Specialist check recommended", "Unresolved"]

SYSTEM = f"""You convert confidential content-ethics review records from a Japanese game company into
anonymized, generic English precedent records. The records will be used by an assistant that advises
OTHER internal teams, and every case is confidential, so the output must not let anyone identify the
game, team, company, people, or the specific case.

REMOVE OR GENERALIZE (never output these):
- Game titles, series, project names, codenames, abbreviations (e.g. three-letter studio codes)
- Company, studio, department and team names; names of people
- Case numbers ("No.32"), dates, chapter/episode numbers, scene IDs, asset or file names (e.g. crw_c51_1000)
- Fictional in-game place names, character names, shop/brand names invented for the game
- Real-world place names when they would identify the game's setting; replace with a generic
  description ("a downtown district in a Japan-set game") unless the real place IS the ethics issue
  (e.g. a real religious site, a real disputed territory), in which case keep it
- Names of internal tools, wikis, mailing lists or review processes

KEEP (this is what makes the precedent useful):
- What kind of content it was (UI icon, signage, dialogue line, costume, enemy design, cutscene, ...)
- The real-world symbol, word, gesture, group or theme at issue (red cross, hammer and sickle, a slur,
  a religious figure, etc.) — if the exact wording under review matters, put it in "reviewed_term"
  in its original language (e.g. a Japanese word) with no surrounding game context
- The context that drove the decision (mature vs. teen audience, villain vs. sympathetic portrayal,
  decorative vs. symbolic use, whether players can see it, narrative purpose)
- Which regions the concern applies to, the verdict, the reasoning, and the recommended fix
- The kinds of specialist checks requested (e.g. "IP/legal team", "regional marketing team") —
  generically, no team names

Write concise, neutral English. Output ONLY a JSON object, no prose, no code fences:
{{
  "category": "short English ethics category, e.g. Red Cross emblem, Communist symbols, Discriminatory language",
  "content_type": "e.g. UI icon / background signage / dialogue / character design / cutscene",
  "regions": ["Asia" and/or "West" — from the record's region flags and content; [] if unclear],
  "issue": "2-4 sentences: what was reviewed and why it raised a question, fully anonymized",
  "reviewed_term": "original-language word/phrase if exact wording was the issue, else null",
  "verdict": one of {json.dumps(VERDICTS)},
  "reasoning": "2-4 sentences: why the reviewers reached that verdict",
  "recommendation": "1-3 sentences: what the team was advised to do (or 'No change needed.')",
  "specialist_checks": ["generic specialist checks recommended, or empty"],
  "summary": "one line, max 20 words, for an index"
}}"""


# --------------------------------------------------------------------------- reading
def read_cases(path: Path) -> tuple[list[dict], set[str]]:
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    header_row = next(i for i, r in enumerate(rows) if r and any(str(v or "").strip() == "No." for v in r))
    header = [re.sub(r"\s+", "", str(v or "")) for v in rows[header_row]]

    pos = {}
    for key, (jp, fallback) in COLUMNS.items():
        jp_norm = re.sub(r"\s+", "", jp)
        pos[key] = header.index(jp_norm) if jp_norm in header else fallback

    def cell(r, key):
        i = pos[key]
        v = r[i] if i < len(r) else None
        return "" if v is None else str(v).strip()

    cases, blocked = [], set()
    for r in rows[header_row + 1:]:
        if not r or not cell(r, "no"):
            continue
        rec = {k: cell(r, k) for k in COLUMNS}
        if not rec["unit_reply"]:
            continue  # unanswered case: no precedent yet
        for k in IDENTIFYING:
            add_terms(blocked, rec[k])
        cases.append(rec)
    return cases, blocked


def add_terms(blocked: set, value: str):
    """Add a column value and its distinctive parts to the blocked-term set.
    Japanese values (names, departments) are split into parts; English/romaji values are only
    split into parts that look like codes or brands (ALLCAPS, digits, CamelCase), so ordinary
    words inside a title ("Cross", "Dark", "Edge") don't block real ethics vocabulary."""
    value = (value or "").strip()
    if not value or value.startswith("="):
        return
    blocked.add(value)
    for part in re.split(r"[\s　/／・,、()（）]+", value):
        if len(part) < 2:
            continue
        if re.fullmatch(r"[A-Za-z0-9.&'-]+", part):
            if (part.isupper() or re.search(r"\d", part) or re.search(r"[a-z][A-Z]", part)):
                blocked.add(part)
        else:
            blocked.add(part)


GENERIC_PARTS = {"チーム", "部", "課", "本部", "株式会社", "部門", "グループ", "その他", "一般", "コラボ",
                 "画集", "倫理", "法務課", "法務部", "ユニット", "プロジェクト", "事業部", "品質管理"}


def is_ascii(term: str) -> bool:
    return bool(re.fullmatch(r"[\x20-\x7e]+", term))


def is_weak(term: str) -> bool:
    return bool(re.fullmatch(r"[A-Z][a-z]+", term))


def find_leaks(record: dict, terms: list[str]) -> list[str]:
    text = re.sub(r"\s+", " ", json.dumps(record, ensure_ascii=False))
    hits = []
    for t in terms:
        if t in GENERIC_PARTS:
            continue
        if is_ascii(t):  # English/romaji: case-sensitive, whole word
            if re.search(rf"(?<![A-Za-z0-9]){re.escape(t)}(?![A-Za-z0-9])", text):
                hits.append(t)
        elif t in text:
            hits.append(t)
    return hits


# --------------------------------------------------------------------------- model
class Sanitizer:
    def __init__(self, model, region, profile, cache_path: Path):
        import boto3
        from botocore.config import Config
        session = boto3.Session(profile_name=profile or None, region_name=region or None)
        self.client = session.client("bedrock-runtime", config=Config(
            read_timeout=300, retries={"max_attempts": 8, "mode": "adaptive"}))
        self.model = model
        self.cache_path = cache_path
        self.cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
        self.lock = threading.Lock()

    def call(self, user: str) -> dict:
        body = json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 8000,
            "system": SYSTEM,
            "messages": [{"role": "user", "content": user}],
        })
        resp = self.client.invoke_model(modelId=self.model, body=body,
                                        contentType="application/json", accept="application/json")
        out = json.loads(resp["body"].read())
        text = "".join(b.get("text", "") for b in out.get("content", []) if b.get("type") == "text")
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
        return json.loads(text[text.find("{"): text.rfind("}") + 1])

    def sanitize(self, rec: dict, row_terms: list[str], all_terms: list[str]) -> dict:
        key = hashlib.sha256(json.dumps(rec, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        with self.lock:
            if key in self.cache:
                return self.cache[key]
        regions = [n for n, k in (("Asia", "asia"), ("West", "west")) if rec[k]]
        if rec["both"]:
            regions = ["Asia", "West"]
        user = (
            "Names that must NOT appear in the output in any form (original, translated or romanized): "
            + json.dumps(row_terms, ensure_ascii=False) + "\n\n"
            "Record (Japanese):\n"
            + json.dumps({
                "ethics_category": rec["category"],
                "content_under_review": rec["content"],
                "review_unit_response": rec["unit_reply"],
                "requesting_team_follow_up": rec["dept_reply"][:600],
                "key_points_for_judgment": rec["key_points"],
                "region_flags": regions,
            }, ensure_ascii=False, indent=1)
        )
        result, leaks = None, []
        for attempt in range(3):
            try:
                msg = user if not leaks else (
                    user + "\n\nYour previous output still contained these forbidden terms: "
                    + json.dumps(leaks, ensure_ascii=False) + ". Remove or generalize them.")
                result = self.call(msg)
                leaks = find_leaks(result, all_terms)
                if not leaks:
                    break
            except Exception as e:  # noqa: BLE001
                print(f"  retrying after error: {type(e).__name__}: {e}")
                time.sleep(4 * (attempt + 1))
        if result is None:
            return {"_error": True}
        if result.get("verdict") not in VERDICTS:
            result["verdict"] = "Unresolved"
        result["_leaks"] = leaks
        with self.lock:
            self.cache[key] = result
            tmp = self.cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.cache, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.cache_path)
        return result


# --------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", help="original (Japanese) case-log .xlsx")
    ap.add_argument("-o", "--outdir")
    ap.add_argument("--region")
    ap.add_argument("--profile")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--extra-terms", help="text file, one blocked term per line")
    ap.add_argument("--include-flagged", action="store_true",
                    help="keep records that still contain a blocked term (not recommended)")
    a = ap.parse_args()

    src = Path(a.input)
    outdir = Path(a.outdir) if a.outdir else src.parent
    outdir.mkdir(parents=True, exist_ok=True)

    cases, blocked = read_cases(src)
    if a.extra_terms:
        for line in Path(a.extra_terms).read_text(encoding="utf-8").splitlines():
            add_terms(blocked, line)
    # Single ordinary-looking English words ("Cross", "Dark") are still passed to the model per
    # case as names to remove, but are not used for automatic leak detection or redaction —
    # otherwise genuine ethics vocabulary (a religious cross) would be flagged everywhere.
    all_terms = sorted((t for t in blocked if t not in GENERIC_PARTS and not is_weak(t)),
                       key=len, reverse=True)
    print(f"{len(cases)} answered cases, {len(all_terms)} blocked terms")

    san = Sanitizer(a.model, a.region, a.profile, src.with_name(src.stem + ".sanitize_cache.json"))
    results: list[dict | None] = [None] * len(cases)

    def work(i):
        rec = cases[i]
        row_terms = set()
        for k in IDENTIFYING:
            add_terms(row_terms, rec[k])
        return i, san.sanitize(rec, sorted(row_terms), all_terms)

    with ThreadPoolExecutor(a.workers) as pool:
        futures = [pool.submit(work, i) for i in range(len(cases))]
        for n, f in enumerate(as_completed(futures), 1):
            i, res = f.result()
            results[i] = res
            if n % 10 == 0 or n == len(cases):
                print(f"  {n}/{len(cases)} cases processed", flush=True)

    kept, flagged, failed = [], [], 0
    for res in results:
        if not res or res.get("_error"):
            failed += 1
            continue
        (flagged if res.get("_leaks") else kept).append(res)
    if a.include_flagged:
        kept += flagged

    # New random IDs and shuffled order, so neither reveals chronology or the original case number.
    random.shuffle(kept)
    out_cases = []
    for n, res in enumerate(kept, 1):
        rec = {k: v for k, v in res.items() if not k.startswith("_")}
        rec["id"] = f"C{n:03d}"
        out_cases.append(rec)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "case_count": len(out_cases),
        "cases": out_cases,
        # Used by the app ONLY to redact accidental leaks from answers; never sent to the model.
        "redaction_terms": all_terms,
    }
    out_json = outdir / "cases_sanitized.json"
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    from cryptography.fernet import Fernet
    key_file = outdir / "cases_encryption_key.txt"
    if key_file.exists():
        key = key_file.read_text(encoding="utf-8").strip().encode()
    else:
        key = Fernet.generate_key()
        key_file.write_text(key.decode() + "\n", encoding="utf-8")
        print(f"Created a new encryption key in {key_file.name} — add it to the app's secrets "
              f"as CASES_ENCRYPTION_KEY.")
    out_enc = outdir / "cases_sanitized.enc"
    out_enc.write_bytes(Fernet(key).encrypt(out_json.read_bytes()))

    out_csv = outdir / "cases_review.csv"
    fields = ["id", "category", "content_type", "regions", "verdict", "summary", "issue",
              "reviewed_term", "reasoning", "recommendation", "specialist_checks"]
    with out_csv.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for c in out_cases:
            w.writerow({k: (", ".join(v) if isinstance(v, list) else v) for k, v in c.items()})

    print(f"\nWrote {out_enc.name} (commit this), {out_json.name} and {out_csv.name} "
          f"({len(out_cases)} cases)")
    if flagged:
        print(f"{len(flagged)} cases still contained a blocked term after retries and were "
              f"{'KEPT (--include-flagged)' if a.include_flagged else 'left out'}.")
    if failed:
        print(f"{failed} cases failed to process; re-run to retry them.")
    print("Spot-check cases_review.csv before deploying.")


if __name__ == "__main__":
    main()
