# app.py
# Streamlit suburb tool (AU) with:
# - ABS QuickStats (POA) for median household income + (best-effort) tenure
# - SQM Research (postcode) for vacancy + stock on market (best-effort parsing)
# - NSW DA approvals (Data.NSW CKAN datastore)
# - VIC median house price + 10-year growth cycle (Data.Vic open dataset 2013–2023)
#
# Notes:
# - National suburb-level median house price time series is not freely available from ABS.
# - VIC provider is implemented using Victorian Gov open data.
# - NSW DA implemented using Data.NSW datastore_search (resource_id-based).

import math
import re
from datetime import date, datetime, timedelta

import pandas as pd
import requests
import streamlit as st

# -----------------------------
# Page setup
# -----------------------------
st.set_page_config(page_title="AU Suburb Analyzer", layout="wide")
st.title("AU Suburb Analyzer")

# -----------------------------
# Constants / sources
# -----------------------------
ABS_QS_BASE = "https://www.abs.gov.au/census/find-census-data/quickstats/2021"
# Postcode QuickStats uses POA + 4 digits, e.g. POA3305
# POA coding matches postcode digits. :contentReference[oaicite:6]{index=6}

# SQM endpoints (postcode-based)
SQM_VACANCY_URL = "https://sqmresearch.com.au/graph_vacancy.php?postcode={postcode}&t=1"
SQM_LISTINGS_URL = "https://sqmresearch.com.au/total-property-listings.php?postcode={postcode}&t=1"

# VIC median house by suburb time series (XLSX)
# Dataset page: :contentReference[oaicite:7]{index=7}
VIC_HOUSES_XLSX_URL = "https://www.land.vic.gov.au/__data/assets/excel_doc/0029/709751/Houses-by-suburb-2013-2023.xlsx"

# NSW DA datastore (Data.NSW). Resource id discovered on dataset page. :contentReference[oaicite:8]{index=8}
NSW_DA_DATASTORE_SEARCH = "https://data.nsw.gov.au/data/api/action/datastore_search"
NSW_DA_RESOURCE_ID = "f01e16bc-c8ca-4feb-a99b-161ec347795c"


# -----------------------------
# Helpers
# -----------------------------
def money_to_float(s: str) -> float | None:
    if not s:
        return None
    s = s.strip()
    m = re.sub(r"[^\d.]", "", s)
    try:
        return float(m)
    except:
        return None


def amortisation_years(principal: float, annual_rate: float, monthly_payment: float) -> float | None:
    """Return years to amortise; None if not amortisable."""
    r = annual_rate / 12.0
    if principal <= 0:
        return 0.0
    if monthly_payment <= principal * r:
        return None  # payment doesn't cover interest
    n_months = math.log(monthly_payment / (monthly_payment - r * principal)) / math.log(1 + r)
    return n_months / 12.0


def pct_change(a: float, b: float) -> float | None:
    # return (a/b -1)*100
    if b is None or b == 0 or a is None:
        return None
    return (a / b - 1.0) * 100.0


# -----------------------------
# ABS QuickStats (POA postcode) parser
# -----------------------------
@st.cache_data(show_spinner=False, ttl=60 * 60 * 24)
def fetch_abs_quickstats_poa(postcode: str) -> dict:
    """
    Returns dict with:
    - median_weekly_household_income
    - (best-effort) tenure shares: owned_outright, owned_mortgage, rented
    """
    postcode = postcode.strip()
    url = f"{ABS_QS_BASE}/POA{postcode}"
    html = requests.get(url, timeout=30).text

    out = {
        "url": url,
        "median_weekly_household_income": None,
        "tenure": {
            "owned_outright": None,
            "owned_with_mortgage": None,
            "rented": None,
        },
    }

    # Median weekly household income (shown in the summary section)
    # Example visible on POA3305 page. :contentReference[oaicite:9]{index=9}
    m = re.search(r"Median weekly household income\s*\$([\d,]+)", html)
    if m:
        out["median_weekly_household_income"] = money_to_float(m.group(1))

    # Tenure is not always reliably present in the same HTML layout.
    # We'll do a best-effort parse by looking for common labels + percentages or counts.
    # If it fails, we return None and the UI will show "Not found in QuickStats HTML".
    def find_tenure(label: str) -> float | None:
        # Look for patterns like: "Owned outright ... 32.1%" or "Owned outright ... 1,234"
        # We'll prefer % if available.
        # This is heuristic.
        pattern_pct = rf"{re.escape(label)}.*?(\d{{1,3}}(?:\.\d+)?)\s*%"
        mp = re.search(pattern_pct, html, flags=re.S | re.I)
        if mp:
            try:
                return float(mp.group(1))
            except:
                return None
        return None

    out["tenure"]["owned_outright"] = find_tenure("Owned outright")
    out["tenure"]["owned_with_mortgage"] = find_tenure("Owned with a mortgage")
    out["tenure"]["rented"] = find_tenure("Rented")

    return out


# -----------------------------
# SQM parsing (best-effort)
# -----------------------------
def _parse_new_date_format(html: str, value_col: str) -> pd.DataFrame:
    rows = []
    for m in re.finditer(r"new Date\((\d{4}),\s*(\d{1,2}),\s*(\d{1,2})\)\s*,\s*([0-9.]+)", html):
        y, mo, d, v = m.groups()
        dt = pd.Timestamp(year=int(y), month=int(mo) + 1, day=int(d))  # JS months often 0-indexed
        rows.append((dt, float(v)))
    df = pd.DataFrame(rows, columns=["date", value_col]).drop_duplicates()
    return df.sort_values("date") if not df.empty else df


def _parse_highcharts_categories_series(html: str, value_col: str) -> pd.DataFrame:
    cat_m = re.search(r"categories\s*:\s*\[(.*?)\]\s*,", html, flags=re.S)
    if not cat_m:
        return pd.DataFrame(columns=["date", value_col])

    cats_raw = cat_m.group(1)
    cats = re.findall(r'"([^"]+)"', cats_raw)

    data_blocks = re.findall(r"data\s*:\s*\[(.*?)\]", html, flags=re.S)
    best = None
    best_count = 0
    for block in data_blocks:
        nums = re.findall(r"-?\d+(?:\.\d+)?", block)
        if len(nums) > best_count:
            best = nums
            best_count = len(nums)

    if not best or len(cats) == 0:
        return pd.DataFrame(columns=["date", value_col])

    n = min(len(cats), len(best))

    dates = []
    for i in range(n):
        # SQM often uses "Jan-05" style
        try:
            dates.append(pd.to_datetime(cats[i], format="%b-%y"))
        except Exception:
            dates.append(pd.to_datetime(cats[i], errors="coerce"))
    values = [float(x) for x in best[:n]]

    df = pd.DataFrame({"date": dates, value_col: values}).dropna(subset=["date"])
    return df.sort_values("date") if not df.empty else df


@st.cache_data(show_spinner=False, ttl=60 * 60 * 12)
def fetch_sqm_series(url: str, value_col: str) -> pd.DataFrame:
    html = requests.get(url, timeout=30).text

    # maintenance detection
    if "under maintenance" in html.lower():
        return pd.DataFrame(columns=["date", value_col])

    # try old format first
    df = _parse_new_date_format(html, value_col)
    if not df.empty:
        return df

    # then try highcharts-style parse
    df = _parse_highcharts_categories_series(html, value_col)
    return df


# -----------------------------
# NSW DA approvals (Data.NSW CKAN datastore)
# -----------------------------
@st.cache_data(show_spinner=False, ttl=60 * 60 * 6)
def fetch_nsw_da_counts(postcode: str, months_back: int = 12) -> pd.DataFrame:
    """
    Pull DA records from Data.NSW datastore and count per month (lodged + determined best-effort).
    This uses 'datastore_search' endpoint. :contentReference[oaicite:10]{index=10}
    """
    postcode = postcode.strip()

    # Pull a chunk and filter locally.
    # Using q=postcode is imperfect but works as a first pass.
    params = {
        "resource_id": NSW_DA_RESOURCE_ID,
        "q": postcode,
        "limit": 10000,
    }
    j = requests.get(NSW_DA_DATASTORE_SEARCH, params=params, timeout=45).json()
    records = j.get("result", {}).get("records", [])
    if not records:
        return pd.DataFrame(columns=["month", "lodged", "determined"])

    df = pd.DataFrame(records)

    # Try to find best date fields (dataset schema can evolve)
    # We'll look for common candidates
    date_cols = [c for c in df.columns if "date" in c.lower()]
    # Heuristic picks
    lodged_candidates = [c for c in date_cols if "lodg" in c.lower() or "submitted" in c.lower() or "created" in c.lower()]
    determined_candidates = [c for c in date_cols if "determ" in c.lower() or "decis" in c.lower() or "final" in c.lower()]

    def pick_first(cols: list[str]) -> str | None:
        return cols[0] if cols else None

    lodged_col = pick_first(lodged_candidates) or pick_first(date_cols)
    determined_col = pick_first(determined_candidates)

    # Filter to postcode if there is a postcode-like field
    pc_cols = [c for c in df.columns if "post" in c.lower()]
    if pc_cols:
        # keep rows where any postcode col matches
        mask = False
        for c in pc_cols:
            mask = mask | (df[c].astype(str).str.contains(postcode, na=False))
        df = df[mask]

    # Parse dates
    def to_dt(series: pd.Series) -> pd.Series:
        return pd.to_datetime(series, errors="coerce", utc=False)

    if lodged_col:
        df["_lodged_dt"] = to_dt(df[lodged_col])
    else:
        df["_lodged_dt"] = pd.NaT

    if determined_col:
        df["_determ_dt"] = to_dt(df[determined_col])
    else:
        df["_determ_dt"] = pd.NaT

    cutoff = pd.Timestamp.today() - pd.DateOffset(months=months_back)
    df_l = df[df["_lodged_dt"].notna() & (df["_lodged_dt"] >= cutoff)].copy()
    df_d = df[df["_determ_dt"].notna() & (df["_determ_dt"] >= cutoff)].copy()

    def month_bucket(s: pd.Series) -> pd.Series:
        return s.dt.to_period("M").dt.to_timestamp()

    lodged_counts = (
        df_l.assign(month=month_bucket(df_l["_lodged_dt"]))
        .groupby("month")
        .size()
        .rename("lodged")
    )

    determined_counts = (
        df_d.assign(month=month_bucket(df_d["_determ_dt"]))
        .groupby("month")
        .size()
        .rename("determined")
    )

    out = pd.concat([lodged_counts, determined_counts], axis=1).fillna(0).astype(int).reset_index()
    out = out.sort_values("month")
    return out


# -----------------------------
# VIC median house price + growth cycle (suburb input)
# -----------------------------
@st.cache_data(show_spinner=False, ttl=60 * 60 * 24)
def fetch_vic_house_timeseries(suburb: str) -> pd.DataFrame:
    """
    Reads VIC XLSX and returns the 2013-2023 yearly medians for the suburb.
    Dataset described here: :contentReference[oaicite:11]{index=11}
    """
    suburb_norm = suburb.strip().upper()
    # Read excel directly from URL
    df = pd.read_excel(VIC_HOUSES_XLSX_URL)

    # Columns vary but typically include suburb name and yearly median columns.
    # We'll try to detect them.
    # Find suburb column
    possible_suburb_cols = [c for c in df.columns if "suburb" in str(c).lower()]
    if not possible_suburb_cols:
        return pd.DataFrame(columns=["year", "median"])

    suburb_col = possible_suburb_cols[0]
    d = df[df[suburb_col].astype(str).str.upper().str.strip() == suburb_norm].copy()
    if d.empty:
        return pd.DataFrame(columns=["year", "median"])

    # Find year columns like 2013..2023
    year_cols = [c for c in df.columns if re.fullmatch(r"20\d{2}", str(c).strip())]
    if not year_cols:
        # sometimes columns might be '2013 Median' etc.
        year_cols = [c for c in df.columns if re.search(r"20\d{2}", str(c))]

    # Convert to tidy
    row = d.iloc[0]
    rows = []
    for c in year_cols:
        year_m = re.search(r"(20\d{2})", str(c))
        if not year_m:
            continue
        year = int(year_m.group(1))
        val = row[c]
        if pd.isna(val):
            continue
        try:
            median = float(val)
        except:
            median = money_to_float(str(val))
        if median is not None:
            rows.append((year, median))

    out = pd.DataFrame(rows, columns=["year", "median"]).drop_duplicates().sort_values("year")
    return out


def classify_growth_cycle_yearly(ts: pd.DataFrame) -> str:
    """
    Classify based on yearly medians:
    - 1y change and 3y change
    """
    if ts is None or ts.empty or len(ts) < 4:
        return "Unknown (insufficient yearly points)"
    ts = ts.sort_values("year")
    last = ts.iloc[-1]["median"]
    prev = ts.iloc[-2]["median"]
    prev3 = ts.iloc[-4]["median"]

    ch1 = pct_change(last, prev)
    ch3 = pct_change(last, prev3)
    if ch1 is None or ch3 is None:
        return "Unknown"

    # Simple regime rules
    if ch1 > 0 and ch3 > 0 and ch1 >= 0:
        if ch1 >= (ch3 / 3) * 0.8:
            return f"Upswing (1y {ch1:.1f}%, 3y {ch3:.1f}%)"
        return f"Positive but slowing (1y {ch1:.1f}%, 3y {ch3:.1f}%)"
    if ch1 < 0 and ch3 > 0:
        return f"Slowdown / pullback (1y {ch1:.1f}%, 3y {ch3:.1f}%)"
    if ch1 > 0 and ch3 < 0:
        return f"Recovery (1y {ch1:.1f}%, 3y {ch3:.1f}%)"
    if ch1 < 0 and ch3 < 0:
        return f"Downturn (1y {ch1:.1f}%, 3y {ch3:.1f}%)"
    return f"Mixed (1y {ch1:.1f}%, 3y {ch3:.1f}%)"


# -----------------------------
# Sidebar inputs
# -----------------------------
with st.sidebar:
    st.header("Inputs")

    suburb = st.text_input("Suburb (used for VIC price/growth)", placeholder="e.g., Portland")
    state = st.selectbox("State", ["VIC", "NSW", "QLD", "SA", "WA", "TAS", "ACT", "NT"], index=0)
    postcode = st.text_input("Postcode (required for ABS + SQM + DA)", placeholder="e.g., 3305")

    st.divider()
    st.subheader("Affordability assumptions")
    interest_rate = st.number_input("Interest rate (annual %)", min_value=0.0, value=6.5, step=0.1) / 100.0
    deposit_pct = st.number_input("Deposit (%)", min_value=0.0, max_value=100.0, value=20.0, step=1.0) / 100.0
    affordability_pct = st.number_input("Max repayment as % of income", min_value=0.0, max_value=100.0, value=30.0, step=1.0) / 100.0

    st.caption("Tip: Postcode is required. VIC growth cycle requires a suburb name too.")


if not postcode or not re.fullmatch(r"\d{4}", postcode.strip()):
    st.warning("Enter a valid 4-digit postcode to run the tool.")
    st.stop()

postcode = postcode.strip()

# -----------------------------
# 1) ABS: income + tenure
# -----------------------------
abs_data = fetch_abs_quickstats_poa(postcode)
median_weekly_income = abs_data.get("median_weekly_household_income")

col1, col2, col3 = st.columns(3)

with col1:
    st.subheader("Median household income (ABS, 2021)")
    if median_weekly_income is None:
        st.error("Could not parse median weekly household income from ABS QuickStats.")
    else:
        st.metric("Median weekly household income", f"${median_weekly_income:,.0f}")
        st.caption(f"Source: ABS QuickStats POA{postcode}")

with col2:
    st.subheader("Renter to owner ratio (ABS, 2021)")
    tenure = abs_data.get("tenure", {})
    owned_outright = tenure.get("owned_outright")
    owned_mortgage = tenure.get("owned_with_mortgage")
    rented = tenure.get("rented")

    if owned_outright is None and owned_mortgage is None and rented is None:
        st.info("Not found in QuickStats HTML for this postcode (ABS page layout varies).")
        st.caption(f"QuickStats URL: {abs_data.get('url')}")
    else:
        # If % values exist, show them
        owners = 0.0
        renters = 0.0
        if owned_outright is not None:
            owners += owned_outright
        if owned_mortgage is not None:
            owners += owned_mortgage
        if rented is not None:
            renters = rented

        if owners > 0:
            ratio = renters / owners if owners else None
            st.metric("Owner % (outright+mortgage)", f"{owners:.1f}%")
            st.metric("Renter %", f"{renters:.1f}%")
            if ratio is not None:
                st.metric("Renter : Owner", f"{ratio:.2f}")

with col3:
    st.subheader("Affordability (years at 30% income)")
    if median_weekly_income is None:
        st.info("Need ABS income to compute affordability.")
    else:
        # Affordability needs a price. We'll use VIC dataset if VIC+suburb is available.
        st.write("This uses your selected rate/deposit and 30% of median household income.")
        # We'll compute below once we have price.


st.divider()

# -----------------------------
# 2) VIC median price + growth cycle (if VIC)
# -----------------------------
vic_ts = pd.DataFrame()
median_house_price = None

if state == "VIC" and suburb.strip():
    with st.spinner("Loading VIC median house price time series (2013–2023)..."):
        vic_ts = fetch_vic_house_timeseries(suburb)

    if vic_ts.empty:
        st.warning("Could not find VIC suburb in the VIC dataset (check spelling).")
    else:
        median_house_price = float(vic_ts.iloc[-1]["median"])  # latest year in file
        cycle = classify_growth_cycle_yearly(vic_ts)

        cA, cB = st.columns([2, 1])
        with cA:
            st.subheader("Median house price (VIC open data)")
            st.metric("Latest available median (from dataset)", f"${median_house_price:,.0f}")
            st.caption("VIC dataset is yearly medians for 2013–2023 (latest in file).")

            chart_df = vic_ts.copy()
            chart_df["year"] = chart_df["year"].astype(int)
            st.line_chart(chart_df.set_index("year")["median"])

        with cB:
            st.subheader("Growth cycle (VIC)")
            st.success(cycle)

            # Extra: 10y-ish CAGR based on available range
            first = vic_ts.iloc[0]["median"]
            last = vic_ts.iloc[-1]["median"]
            years = int(vic_ts.iloc[-1]["year"] - vic_ts.iloc[0]["year"])
            if years > 0 and first and last:
                cagr = (last / first) ** (1 / years) - 1
                st.metric("Approx CAGR (range)", f"{cagr*100:.2f}%")

else:
    st.subheader("Median house price + Growth cycle")
    st.info(
        "Automatic suburb-level median house price + 10-year growth cycle is implemented for **Victoria** "
        "using Victorian Government open data. For other states you’ll need a commercial provider "
        "(e.g. PropTrack/CoreLogic)."
    )

st.divider()

# -----------------------------
# 3) Affordability calculation (needs income + a price)
# -----------------------------
st.subheader("Affordability result")

if median_weekly_income is None:
    st.error("Cannot compute affordability because ABS median household income was not available.")
else:
    annual_income = median_weekly_income * 52
    monthly_income = annual_income / 12
    max_payment = monthly_income * affordability_pct

    if median_house_price is None:
        st.warning("Cannot compute affordability because median house price is not available for this state/suburb (currently automated for VIC only).")
    else:
        loan = median_house_price * (1 - deposit_pct)
        years = amortisation_years(loan, interest_rate, max_payment)

        c1, c2, c3 = st.columns(3)
        with c1:
            st.metric("Median price (used)", f"${median_house_price:,.0f}")
        with c2:
            st.metric("Max monthly repayment", f"${max_payment:,.0f}")
        with c3:
            st.metric("Years to repay", "Not serviceable" if years is None else f"{years:.1f}")

st.divider()

# -----------------------------
# 4) Vacancy + stock on market (SQM)
# -----------------------------
st.subheader("Vacancy + Stock on market (SQM)")

vac_url = SQM_VACANCY_URL.format(postcode=postcode)
list_url = SQM_LISTINGS_URL.format(postcode=postcode)

cV, cS = st.columns(2)

with cV:
    st.markdown("### Vacancy rate")
    try:
        vac = fetch_sqm_series(vac_url, "vacancy_rate")
        if vac.empty:
            st.warning("Couldn’t parse a vacancy time series (SQM page format or access may have changed).")
            st.caption(f"SQM URL: {vac_url}")
        else:
            st.line_chart(vac.set_index("date")["vacancy_rate"])
            st.metric("Latest vacancy", f"{vac.iloc[-1]['vacancy_rate']:.2f}%")
    except Exception as e:
        st.error(f"Vacancy fetch failed: {e}")
        st.caption(f"SQM URL: {vac_url}")

with cS:
    st.markdown("### Stock on market (total listings)")
    try:
        som = fetch_sqm_series(list_url, "total_listings")
        if som.empty:
            st.warning("Couldn’t parse a listings time series (SQM page format or access may have changed).")
            st.caption(f"SQM URL: {list_url}")
        else:
            st.line_chart(som.set_index("date")["total_listings"])
            st.metric("Latest total listings", f"{som.iloc[-1]['total_listings']:.0f}")

            if len(som) >= 6:
                recent = som["total_listings"].tail(3).mean()
                prior = som["total_listings"].iloc[-6:-3].mean()
                ch = pct_change(recent, prior)
                if ch is not None:
                    st.caption(f"3-month avg vs prior 3-month: {ch:.1f}%")
    except Exception as e:
        st.error(f"Listings fetch failed: {e}")
        st.caption(f"SQM URL: {list_url}")

st.divider()

# -----------------------------
# 5) DA approvals (NSW only)
# -----------------------------
st.subheader("DA approvals / activity (NSW)")

if state != "NSW":
    st.info("Automated DA activity is implemented for NSW (Planning Portal open data via Data.NSW datastore).")
else:
    try:
        da = fetch_nsw_da_counts(postcode, months_back=12)
        if da.empty:
            st.warning("No DA records returned for this postcode (or schema changed).")
        else:
            st.line_chart(da.set_index("month")[["lodged", "determined"]])
            st.dataframe(da, use_container_width=True)
            st.caption("Source: Data.NSW Online DA Data API datastore_search.")
    except Exception as e:
        st.error(f"NSW DA fetch failed: {e}")
        st.caption(f"Endpoint: {NSW_DA_DATASTORE_SEARCH}")

st.divider()

# -----------------------------
# Footer / source notes
# -----------------------------
st.caption(
    "Data sources used: ABS QuickStats (2021) for postcode median household income; "
    "SQM Research for vacancy and listings; NSW DA from Data.NSW datastore; "
    "VIC median house prices from Victorian Government open data (2013–2023)."
)
