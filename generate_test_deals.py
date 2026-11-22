"""Builds 25 synthetic deal submissions exercising the routing-engine design
matrix (see routing_engine.py / the "Vaero Routing Engine" design memory):
every aircraft taxonomy tier, both PG buckets, all three charter states,
all three construction-financing answers, and one deliberately unclassifiable
aircraft model. Doesn't touch the network or the DB itself -- just produces
the list of submission payloads; posting them is a separate step (see
seed_test_deals.py). Financial figures are a single reused strong HNW profile,
scaled loosely by aircraft price -- credit strength isn't the variable under
test here, routing is.

Deliberately a standalone file, not an edit to seed_deals.py -- that file had
unrelated uncommitted local changes already in progress.
"""

EMAIL_DOMAIN = "test.example"


def _financial(purchase_price, weak=False):
    liquid = max(purchase_price * (0.4 if weak else 1.8), 3_000_000 if not weak else 400_000)
    net_worth = max(purchase_price * (1.1 if weak else 3.5), 8_000_000 if not weak else 1_200_000)
    return {
        "recurringCash": round(purchase_price * 0.22),
        "liquidAssets": round(liquid),
        "totalAssets": round(net_worth * 1.4),
        "netWorth": round(net_worth),
        "existingDebt": round(purchase_price * 0.02),
        "agi": round(purchase_price * 0.35),
        "federalTaxesPaid": round(purchase_price * 0.10),
        "k1Detail": [],
        "liquidDetail": [
            {"institution": "Test Private Bank", "accountType": "Investment",
             "balance": round(liquid), "pledged": False, "marginLoanBalance": 0, "note": ""},
        ],
        "debtDetail": [],
        "trustStructures": [],
    }


def _submission(idx, label, make, model, price, pg_type,
                 charter_operator_type="", charter_pct=0,
                 aircraft_condition="pre_owned", construction_financing_needed="",
                 weak_financials=False, hangar_ident="Austin, TX (AUS)"):
    loan_amount = round(price * 0.75)
    aircraft = {
        "year": 2019,
        "make": make,
        "model": model,
        "serial": f"TST{idx:03d}",
        "registration": f"N{100+idx}TX",
        "aftt": 1500,
        "engineProgram": "Enrolled",
        "purchasePrice": price,
        "hangarIdent": hangar_ident,
        "thirdPartyCharter": "yes" if charter_operator_type else "no",
        "charterPct": charter_pct,
        "charterOperatorType": charter_operator_type,
        "aircraftCondition": aircraft_condition,
        "constructionFinancingNeeded": construction_financing_needed,
    }
    return {
        "borrowerType": "individual",
        "transactionType": "purchase",
        "pgType": pg_type,
        "guarantorType": "borrower",
        "principalPGAvailable": "yes" if pg_type == "full_pg" else "",
        "personal": {
            "firstName": "TEST",
            "lastName": f"Routing{idx:02d}",
            "email": f"routing-test-{idx:02d}@{EMAIL_DOMAIN}",
            "occupation": "Test Fixture",
            "state": "TX",
        },
        "aircraft": aircraft,
        "financial": _financial(price, weak=weak_financials),
        "loanPrefs": {
            "loanAmount": loan_amount,
            "preferredTerm": "120",
            "structureType": "balloon",
            "rateTypePreference": "fixed",
            "priorityRanking": ["rate", "advance", "amortization", "prepayment"],
        },
        "_label": label,  # stripped before POSTing; for the seeding report only
    }


SCENARIOS = [
    _submission(1, "VLJ / full PG / no charter", "Cessna", "Citation CJ2+", 3_200_000, "full_pg"),
    _submission(2, "VLJ / no full PG / no charter (tests full-recourse exclusion)", "Cessna", "Citation CJ2+", 3_200_000, "non_recourse"),
    _submission(3, "Light Jet / full PG / no charter / based NJ", "Bombardier", "Learjet 75", 5_500_000, "full_pg", hangar_ident="Teterboro, NJ (KTEB)"),
    _submission(4, "Light Jet / no full PG / no charter", "Pilatus", "PC-24", 11_000_000, "non_recourse"),
    _submission(5, "Midsize Jet / full PG / no charter", "Cessna", "Citation XLS+", 9_500_000, "full_pg"),
    _submission(6, "Midsize Jet / no full PG / incidental charter 15% / based CA", "Cessna", "Citation Latitude", 14_000_000, "non_recourse", charter_operator_type="incidental", charter_pct=15, hangar_ident="Van Nuys, CA (VNY)"),
    _submission(7, "Super-Mid Jet / full PG / no charter", "Bombardier", "Challenger 350", 22_000_000, "full_pg"),
    _submission(8, "Super-Mid Jet / no full PG / charter-operator financing", "Embraer", "Praetor 600", 23_000_000, "non_recourse", charter_operator_type="operator", charter_pct=80),
    _submission(9, "Large Jet / full PG / no charter / based FL", "Gulfstream", "G450", 18_000_000, "full_pg", hangar_ident="Fort Lauderdale, FL (FXE)"),
    _submission(10, "Large Jet / no full PG / no charter", "Dassault", "Falcon 7X", 24_000_000, "non_recourse"),
    _submission(11, "Ultra Long Range / full PG / no charter / based TN (raw airport code only)", "Gulfstream", "G650", 38_000_000, "full_pg", hangar_ident="BNA"),
    _submission(12, "Ultra Long Range / no full PG / no charter", "Bombardier", "Global 7500", 45_000_000, "non_recourse"),
    _submission(13, "Turboprop / full PG / no charter", "Daher", "TBM 940", 4_500_000, "full_pg"),
    _submission(14, "Turboprop / no full PG / incidental charter 10% / based CA (raw airport code only)", "Beechcraft", "King Air 350", 6_500_000, "non_recourse", charter_operator_type="incidental", charter_pct=10, hangar_ident="VNY"),
    _submission(15, "Piston / full PG / no charter", "Cirrus", "SR22T", 1_100_000, "full_pg"),
    _submission(16, "Piston / no full PG / no charter", "Beechcraft", "Bonanza", 900_000, "non_recourse"),
    _submission(17, "Midsize Jet / full PG / NEW + construction financing YES / based NY", "Cessna", "Citation Latitude", 15_500_000, "full_pg", aircraft_condition="new", construction_financing_needed="yes", hangar_ident="White Plains, NY (HPN)"),
    _submission(18, "Super-Mid Jet / no full PG / NEW + construction financing NO", "Bombardier", "Challenger 350", 23_500_000, "non_recourse", aircraft_condition="new", construction_financing_needed="no"),
    _submission(19, "Large Jet / full PG / NEW + construction financing MAYBE", "Gulfstream", "G500", 27_000_000, "full_pg", aircraft_condition="new", construction_financing_needed="maybe"),
    _submission(20, "Light Jet / no full PG / charter-operator financing", "Embraer", "Phenom 300", 9_800_000, "non_recourse", charter_operator_type="operator", charter_pct=90),
    _submission(21, "Ultra Long Range / no full PG / incidental charter 25% / unparseable Aircraft Base (tests resolution-failure WARNING flag)", "Gulfstream", "G700", 72_000_000, "non_recourse", charter_operator_type="incidental", charter_pct=25, hangar_ident="Based at the family ranch, no formal strip"),
    _submission(22, "Unclassifiable make/model (tests fallback + WARNING flag)", "Skyworks Aviation", "Velocity X1", 10_000_000, "full_pg"),
    _submission(23, "VLJ / full PG / charter-operator financing (edge case)", "Honda", "HondaJet Elite", 5_000_000, "full_pg", charter_operator_type="operator", charter_pct=70),
    _submission(24, "Single-engine Turboprop / NEW, construction question skipped by wizard (blank)", "Pilatus", "PC-12", 5_200_000, "full_pg", aircraft_condition="new", construction_financing_needed=""),
    _submission(25, "Midsize Jet / no full PG / weak financials (checks routing is independent of underwriting flags)", "Hawker Beechcraft", "Hawker 900XP", 7_000_000, "non_recourse", weak_financials=True),
]


if __name__ == "__main__":
    import json
    print(json.dumps([{k: v for k, v in s.items() if k != "_label"} for s in SCENARIOS], indent=2)[:500] + "\n...")
    print(f"\n{len(SCENARIOS)} scenarios built.")
