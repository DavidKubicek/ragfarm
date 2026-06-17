"""
test_xlsx_tables — offline regression for the XLSX structural parser.

Runs xlsx_tables.iter_xlsx against the two fixture workbooks and asserts record
counts, absence of trailing-junk leakage, group-label coverage, and a known-host
round-trip. No Qdrant or embedder needed — exercises the parse path only.

Place fixtures under tests/fixtures/ (or point FIXTURES at them) and run:
    python services/ingester/test_xlsx_tables.py
Exit code 0 = all pass, 1 = any failure.
"""
import os
import re
import sys
import pathlib
import logging

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import xlsx_tables as xt

logging.disable(logging.INFO)

FIXTURES = pathlib.Path(os.environ.get("FIXTURES", "tests/fixtures"))
EPC = FIXTURES / "EPC25_VMs_config.xlsx"
SA  = FIXTURES / "SA_Hosting_infra_VMs.xlsx"

# trailing content that must NEVER appear as a record
JUNK_MARKERS = ["Notes:", "notes:", "pravdepodobne", "Oddělená síť",
                "number of VMs", "100.80.9.251"]

EXPECT_EPC = {"New DC": 33, "Old DC": 21, "Stacked": 13, "Plain": 4}
EXPECT_SA_TOTAL = 41
EXPECT_SA_GROUPS = 13


def _by_sheet(recs):
    out = {}
    for r in recs:
        out.setdefault(r["payload"]["sheet"], []).append(r)
    return out


def run():
    failures = []

    if not EPC.exists() or not SA.exists():
        print(f"FIXTURES missing under {FIXTURES} "
              f"(EPC={EPC.exists()}, SA={SA.exists()})")
        return 1

    epc = list(xt.iter_xlsx(EPC))
    sa = list(xt.iter_xlsx(SA))

    # 1. EPC25 per-sheet counts
    bs = _by_sheet(epc)
    for sheet, want in EXPECT_EPC.items():
        got = len(bs.get(sheet, []))
        ok = got == want
        print(f"[{'PASS' if ok else 'FAIL'}] EPC25/{sheet}: {got} (want {want})")
        if not ok:
            failures.append(f"EPC25/{sheet} count {got}!={want}")

    # 2. SA_Hosting total
    ok = len(sa) == EXPECT_SA_TOTAL
    print(f"[{'PASS' if ok else 'FAIL'}] SA_Hosting/List1: {len(sa)} (want {EXPECT_SA_TOTAL})")
    if not ok:
        failures.append(f"SA total {len(sa)}!={EXPECT_SA_TOTAL}")

    # 3. zero junk across both
    junk = [r["text"][:60] for r in epc + sa
            if any(m in r["text"] for m in JUNK_MARKERS)]
    ok = not junk
    print(f"[{'PASS' if ok else 'FAIL'}] no trailing junk leaked: {len(junk)}")
    if not ok:
        failures.append(f"junk leaked: {junk}")

    # 4. SA group coverage
    groups = set()
    for r in sa:
        m = re.match(r"sheet: [^,]+, group: ([^,]+),", r["text"])
        if m:
            groups.add(m.group(1))
    ok = len(groups) >= EXPECT_SA_GROUPS
    print(f"[{'PASS' if ok else 'FAIL'}] SA groups captured: {len(groups)} (want >= {EXPECT_SA_GROUPS})")
    if not ok:
        failures.append(f"SA groups {len(groups)}<{EXPECT_SA_GROUPS}")

    # 5. known-host round-trip: all three network FQDNs on one record
    h = [r for r in sa if "hsmbvxip001ts" in r["text"]]
    ok = (len(h) == 1
          and "csr01.hs.skoda.vwg" in h[0]["text"]
          and "bck.hs.skoda.vwg" in h[0]["text"]
          and "stg.hs.skoda.vwg" in h[0]["text"]
          and "group: terminal servers" in h[0]["text"])
    print(f"[{'PASS' if ok else 'FAIL'}] hsmbvxip001ts round-trip (3 networks + group)")
    if not ok:
        failures.append("hsmbvxip001ts round-trip failed")

    # 6. Plain bare-value join (no synthetic keys)
    plain = bs.get("Plain", [])
    ok = (len(plain) == 4
          and " | " in plain[0]["text"]
          and "col1:" not in plain[0]["text"])
    print(f"[{'PASS' if ok else 'FAIL'}] Plain bare-value join, identifiers verbatim")
    if not ok:
        failures.append("Plain bare-join failed")

    print()
    if failures:
        print(f"FAILED ({len(failures)}): " + "; ".join(failures))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(run())
