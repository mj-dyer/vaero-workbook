"""Lender-deal matching/routing engine.

Pure functions over data — no DB or HTTP calls in this file. Callers (main.py's
admin routes) are responsible for resolving the deal and lender records and
passing in plain dicts. Design rules implemented here are locked in the
"Vaero Routing Engine" design notes (memory, started 2026-11-20) — see that
doc for the reasoning behind each rule; this file is the literal translation
of those decisions into code, not a reinterpretation of them.
"""

import json
import os
import re

_AIRPORT_STATES_PATH = os.path.join(os.path.dirname(__file__), "airport_states.json")
with open(_AIRPORT_STATES_PATH) as _f:
    # code (ICAO/IATA/local/GPS, uppercase) -> 2-letter US state/territory code.
    # Built from OurAirports' public dataset, filtered to US large/medium/small
    # airports -- see scratchpad generation step; regenerate by re-filtering
    # https://davidmegginson.github.io/ourairports-data/airports.csv the same way.
    AIRPORT_STATE = json.load(_f)

_US_STATES = set(AIRPORT_STATE.values())
_CITY_STATE_RE = re.compile(r",\s*([A-Za-z]{2})\b")
_CODE_TOKEN_RE = re.compile(r"\b[A-Za-z]{3,4}\b")


def resolve_state(hangar_ident):
    """Parses the borrower wizard's free-text "Aircraft Base" field (e.g.
    "Nashville, TN" or "BNA" or "Austin, TX (AUS)") into a 2-letter state
    code. Returns None if it can't be resolved -- callers must never treat
    None as a match failure, only as "can't apply a geography restriction
    here," consistent with the rest of this engine never blocking on
    incomplete data.
    """
    if not hangar_ident:
        return None
    text = hangar_ident.strip()
    m = _CITY_STATE_RE.search(text)
    if m and m.group(1).upper() in _US_STATES:
        return m.group(1).upper()
    for token in _CODE_TOKEN_RE.findall(text):
        state = AIRPORT_STATE.get(token.upper())
        if state:
            return state
    return None

# Six-tier jet taxonomy + piston/turboprop, locked 2026-11-20. Matches the
# slugs already used by aircraftCategoryAppetite in main.py's LenderApplication
# model: piston | turboprop | vlj | light_jet | midsize_jet | super_mid_jet |
# large_jet | ultra_long_range.
#
# Keys are uppercase model-identifying substrings checked against "MAKE MODEL"
# normalized text. Matching picks the longest (most specific) matching key so
# e.g. "LEARJET 45XR" doesn't get caught by a bare "LEARJET 45" entry if both
# existed. Order within each category list doesn't matter.
AIRCRAFT_MODEL_KEYWORDS = {
    "vlj": [
        "CITATION CJ2+", "CITATION CJ2", "CITATION M2", "PHENOM 100",
        "HONDAJET CLASSIC", "HONDAJET ELITE", "HONDAJET", "VISION JET", "SF-50", "SF50",
    ],
    "light_jet": [
        "LEARJET 45XR", "LEARJET 45", "LEARJET 40", "LEARJET 70", "LEARJET 75",
        "CITATION CJ3+", "CITATION CJ3", "CITATION CJ4", "PHENOM 300", "PC-24", "PC24",
    ],
    "midsize_jet": [
        "LEARJET 60XR", "LEARJET 60",
        "CITATION ASCEND", "CITATION EXCEL", "CITATION LATITUDE",
        "CITATION SOVEREIGN+", "CITATION SOVEREIGN", "CITATION X+", "CITATION X",
        "CITATION XLS+", "CITATION XLS",
        "FALCON 50EX", "FALCON 50",
        "PRAETOR 500", "L450",
        "G150",
        "HAWKER 750", "HAWKER 800XP", "HAWKER 850XP", "HAWKER 900XP",
    ],
    "super_mid_jet": [
        "CHALLENGER 300", "CHALLENGER 350", "CHALLENGER 3500",
        "CITATION LONGITUDE",
        "FALCON 2000S",
        "LEGACY 500", "PRAETOR 600",
        "G200", "G280",
        "HAWKER 4000",
    ],
    "large_jet": [
        "CHALLENGER 601-3A", "CHALLENGER 601-3R", "CHALLENGER 604", "CHALLENGER 605", "CHALLENGER 650",
        "GLOBAL 5000", "GLOBAL 5500",
        "FALCON 2000EXY", "FALCON 2000EX", "FALCON 2000DX", "FALCON 2000",
        "FALCON 6X", "FALCON 7X",
        "FALCON 900EXY", "FALCON 900EX", "FALCON 900DX", "FALCON 900C", "FALCON 900B", "FALCON 900",
        "LEGACY 600", "LEGACY 650",
        "G350", "G450", "G500", "GIVSP", "GIV",
    ],
    "ultra_long_range": [
        "GLOBAL 6000", "GLOBAL 6500", "GLOBAL 7500", "GLOBAL 8000",
        "GLOBAL EXPRESS", "GLOBAL XRS",
        "FALCON 8X",
        "G550", "G600", "G650", "G700", "G800", "GV",
    ],
    "turboprop": [
        "PC-12", "PC12",
        "TBM 700", "TBM 850", "TBM 900", "TBM 910", "TBM 930", "TBM 940", "TBM",
        "KING AIR 90", "KING AIR 200", "KING AIR 250", "KING AIR 300", "KING AIR 350", "KING AIR 360", "KING AIR",
        "M500", "M600", "MERIDIAN",
        "CARAVAN",
        "AVANTI",
    ],
    "piston": [
        "SR20", "SR22T", "SR22",
        "CESSNA 172", "CESSNA 182", "CESSNA 206", "CESSNA 210",
        "BONANZA", "BARON",
        "SARATOGA", "MALIBU", "MIRAGE", "SENECA", "SEMINOLE",
        "DA40", "DA42",
        "ACCLAIM", "OVATION",
    ],
}

# Sorted longest-first once, at import time, so classify_aircraft doesn't
# re-sort on every call.
_SORTED_KEYWORDS = sorted(
    ((kw, category) for category, kws in AIRCRAFT_MODEL_KEYWORDS.items() for kw in kws),
    key=lambda pair: len(pair[0]),
    reverse=True,
)

# Fallback when make/model isn't in the known taxonomy at all. Per the design
# doc, the classifier must never leave a deal unclassified or block routing —
# it always assigns a best-guess category. A true "closest comparable by size/
# weight class" judgment call needs real aircraft knowledge per model, which
# isn't practical to hardcode exhaustively; midsize_jet is used as a neutral
# middle-of-the-road guess for genuinely unrecognized jets. This is a known
# simplification — the WARNING flag emitted alongside it is what's supposed
# to catch and correct a bad guess, not the guess itself being perfect.
_FALLBACK_CATEGORY = "midsize_jet"


def classify_aircraft(make, model):
    """Returns (category_slug, is_exact_match)."""
    text = f"{make or ''} {model or ''}".upper()
    for keyword, category in _SORTED_KEYWORDS:
        if keyword in text:
            return category, True
    return _FALLBACK_CATEGORY, False


def match_recourse(pg_type, lender_recourse_pref):
    """Ordinal: non_recourse lenders also accept limited/full; limited lenders
    also accept full; full-only lenders accept only full PG deals.

    pg_type is the borrower's binary wizard answer: "full_pg" or "non_recourse"
    (the latter actually meaning "no full PG" — the wizard deliberately doesn't
    ask borrowers to distinguish limited from non-recourse; see design doc).
    Missing/unrecognized pg_type is treated the same as "non_recourse" — the
    more conservative assumption, since claiming an unverified full PG would
    wrongly admit a deal to full-recourse-only lenders.
    """
    if not lender_recourse_pref:
        return True
    if pg_type == "full_pg":
        return True
    return lender_recourse_pref in ("limited", "non_recourse")


def match_aircraft_category(deal_category, lender_categories):
    if not lender_categories:
        return True
    return deal_category in lender_categories


def match_fractional(is_fractional_share, lender_fractional_appetite):
    if not is_fractional_share:
        return True
    return bool(lender_fractional_appetite)


def match_charter(charter_operator_type, lender_third_party_charter_appetite, lender_charter_operator_appetite):
    """"incidental" = Part 91 with some charter; "operator" = charter income is
    the primary repayment source; "" = no charter usage at all (always matches).
    """
    if charter_operator_type == "incidental":
        return bool(lender_third_party_charter_appetite)
    if charter_operator_type == "operator":
        return bool(lender_charter_operator_appetite)
    return True


def match_construction_financing(aircraft_condition, construction_financing_needed, lender_construction_appetite):
    """Only new-aircraft deals can even ask for construction/progress-payment
    financing. "maybe" is deliberately inclusive of both willing and unwilling
    lenders per the design doc — it's not a middle filter, it's a non-filter.
    """
    if aircraft_condition != "new":
        return True
    if construction_financing_needed == "yes":
        return bool(lender_construction_appetite)
    return True


def match_geography(deal_state, geography_scope, geography_states):
    """National-scope lenders (the default) are never restricted. Regional
    lenders only match deals whose resolved aircraft-base state is in their
    listed states. An unresolved deal_state (couldn't parse "Aircraft Base")
    never excludes the deal -- same never-block philosophy as the aircraft
    classifier's fallback path; the caller surfaces a flag instead.
    """
    if geography_scope != "regional":
        return True
    if deal_state is None:
        return True
    return deal_state in (geography_states or [])


def match_loan_size(loan_amount, deal_size_min, deal_size_max):
    """dealSizeMin/dealSizeMax are real operational limits (underwriting
    capacity, balance-sheet concentration), not a negotiable term -- a lender
    who caps at $20MM genuinely can't/won't process a $100MM deal. Unlike LTV,
    no tolerance band here. Missing min/max on the lender's side is never a
    restriction (never block on incomplete data).
    """
    if deal_size_min is not None and loan_amount < deal_size_min:
        return False
    if deal_size_max is not None and loan_amount > deal_size_max:
        return False
    return True


# Percentage-point tolerance for match_ltv -- one-directional on purpose
# (2026-11-22 decision): a lender is never excluded for being MORE permissive
# than the deal needs, only for being more conservative than the ask by more
# than this many points. Widened from an initial 10 to 15 per Matt -- bias
# toward more lenders seeing the deal on any hard-ish gate.
_LTV_TOLERANCE_POINTS = 15


def match_ltv(deal_ltv_fraction, lender_max_ltv_pct):
    """deal_ltv_fraction is a 0-1 ratio (deals_engine's convention, e.g. 0.90);
    lender_max_ltv_pct is a plain percentage (vaero-partners' convention, e.g.
    90). LTV is a negotiable deal term, not a fixed capability -- a lender
    capped below the ask can still counter with a lower-leverage structure in
    their IOI, so this is a soft gate with a tolerance band, not an exact
    cutoff. Missing lender maxLtv is never a restriction.
    """
    if lender_max_ltv_pct is None:
        return True
    deal_ltv_pct = (deal_ltv_fraction or 0) * 100
    return deal_ltv_pct <= lender_max_ltv_pct + _LTV_TOLERANCE_POINTS


# Recourse tiers ranked for the optional sort-order nicety below — lower rank
# is "closer to full recourse," i.e. less risk absorbed by the lender.
_RECOURSE_RANK = {"full": 0, "limited": 1, "non_recourse": 2}


def match_lenders(deal_aircraft, pg_type, lenders, is_fractional_share=False,
                   loan_amount=None, deal_ltv_fraction=None):
    """
    *** READ THIS BEFORE ADDING A NEW PARAMETER TO THIS FUNCTION ***

    Everything in the `reasons` loop below is a hard eligibility gate -- a
    lender who fails any one of them is excluded from matchedLenders, full
    stop, before ranking/priority/pinning logic (whenever that gets built)
    ever sees them. That ordering is load-bearing: a "pinned" or high-
    priority lender must only ever be able to win a spot that ranking would
    otherwise have given to someone else -- never a spot they weren't
    actually eligible for in the first place (e.g. a lender capped at a
    $10MM max deal size must never get pinned onto a $20MM deal).

    So: any NEW matching dimension (loan size, credit-tier appetite,
    whatever comes next) belongs in THIS function as another hard
    `if not match_X(...): reasons.append(...)` check -- never bolted on
    later as part of a ranking/scoring step. If you're adding a parameter
    anywhere else in the routing pipeline and it could exclude a lender from
    a deal they shouldn't be on, it belongs here, not there.

    The one deliberate exception is LTV (match_ltv): it's a negotiable deal
    term, not a fixed capability, so it's a soft gate with a tolerance band
    rather than an exact cutoff -- see match_ltv's docstring. Credit-quality/
    GDSCR-based matching was discussed and explicitly tabled (2026-11-22) --
    don't add it without that conversation being reopened.

    deal_aircraft: the deal's analysis["aircraft"] dict.
    pg_type: the deal's top-level pgType ("full_pg" | "non_recourse" | "").
    lenders: list of lender record dicts (as returned by deals_store.list_lenders()).
    loan_amount: the deal's requested loan amount (plain dollars), for match_loan_size.
    deal_ltv_fraction: the deal's LTV as a 0-1 ratio (deals_engine's convention), for match_ltv.

    Returns a dict: aircraftCategory, aircraftCategoryExactMatch, matchedLenders
    (sorted), allResults (every lender with matched bool + exclusion reasons),
    flags (list, in the same {severity, title, detail, mitigant} shape as
    deals_engine's existing underwriting flags).
    """
    make = deal_aircraft.get("make", "")
    model = deal_aircraft.get("model", "")
    category, is_exact = classify_aircraft(make, model)
    charter_operator_type = deal_aircraft.get("charterOperatorType", "")
    aircraft_condition = deal_aircraft.get("aircraftCondition", "")
    construction_financing_needed = deal_aircraft.get("constructionFinancingNeeded", "")
    hangar_ident = deal_aircraft.get("hangarIdent", "")
    deal_state = resolve_state(hangar_ident)

    all_results = []
    for lender in lenders:
        if lender.get("status") != "approved":
            continue  # only approved lenders are eligible to be routed deals

        reasons = []
        if not match_recourse(pg_type, lender.get("recoursePreference")):
            reasons.append("Recourse: lender requires Full Recourse Only; deal has no full PG.")
        if not match_aircraft_category(category, lender.get("aircraftCategoryAppetite") or []):
            reasons.append(f"Aircraft category: lender isn't open to {category}.")
        if not match_fractional(is_fractional_share, lender.get("fractionalShareAppetite")):
            reasons.append("Fractional share: lender isn't open to fractional interests.")
        if not match_charter(charter_operator_type, lender.get("thirdPartyCharterAppetite"), lender.get("charterOperatorFinancingAppetite")):
            reasons.append("Charter: lender's charter appetite doesn't cover this deal's charter usage.")
        if not match_construction_financing(aircraft_condition, construction_financing_needed, lender.get("constructionFinancingAppetite")):
            reasons.append("Construction financing: lender isn't open to progress-payment financing.")
        if not match_geography(deal_state, lender.get("geographyScope"), lender.get("geographyStates")):
            reasons.append(f"Geography: lender only serves {', '.join(lender.get('geographyStates') or [])}; aircraft is based in {deal_state}.")
        if loan_amount is not None and not match_loan_size(loan_amount, lender.get("dealSizeMin"), lender.get("dealSizeMax")):
            reasons.append(f"Deal size: lender's range is {lender.get('dealSizeMin')}–{lender.get('dealSizeMax')}; loan amount is {loan_amount}.")
        if deal_ltv_fraction is not None and not match_ltv(deal_ltv_fraction, lender.get("maxLtv")):
            reasons.append(f"LTV: deal LTV of {deal_ltv_fraction*100:.1f}% exceeds lender's maximum of {lender.get('maxLtv')}% by more than the {_LTV_TOLERANCE_POINTS}-point tolerance.")

        all_results.append({
            "lenderId": lender.get("lenderId"),
            "institutionName": lender.get("institutionName"),
            "matched": len(reasons) == 0,
            "reasons": reasons,
        })

    matched_lenders = [r for r in all_results if r["matched"]]

    # Sort-order nicety, not a filter: for no-full-PG deals, rank limited-
    # recourse lenders ahead of true non-recourse ones (limited terms are
    # typically better for the borrower). No effect on full-PG deals since
    # every matched lender's tier is irrelevant there.
    if pg_type != "full_pg":
        matched_lenders.sort(key=lambda r: _RECOURSE_RANK.get(
            next((l.get("recoursePreference") for l in lenders if l.get("lenderId") == r["lenderId"]), ""), 1
        ))

    flags = []
    if not is_exact:
        aircraft_label = f"{(make or '').strip()} {(model or '').strip()}".strip()
        flags.append({
            "severity": "WARNING",
            "title": "Aircraft Category Auto-Classified",
            "detail": f"'{aircraft_label}' isn't in the known taxonomy — best-guess classified as {category}. Confirm or adjust.",
            "mitigant": None,
        })
    if hangar_ident and deal_state is None:
        flags.append({
            "severity": "WARNING",
            "title": "Aircraft Base Couldn't Be Resolved",
            "detail": f"'{hangar_ident}' didn't parse to a US state — regional lenders' geography restriction wasn't applied to this deal. Confirm or adjust Aircraft Base.",
            "mitigant": None,
        })

    return {
        "aircraftCategory": category,
        "aircraftCategoryExactMatch": is_exact,
        "aircraftBaseState": deal_state,
        "matchedLenders": matched_lenders,
        "allResults": all_results,
        "flags": flags,
    }
