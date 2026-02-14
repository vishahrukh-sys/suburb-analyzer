# app.py
import re
import math
import json
import requests
import pandas as pd
import streamlit as st
from bs4 import BeautifulSoup

st.set_page_config(page_title="Suburb Analyzer (AU)", layout="wide")

# -----------------------------
# Utilities
# -----------------------------
def amortisation_years(principal: float, annual_rate: float, monthly_payment: float) -> float | None:
    """Return years to amortise; None if not amortisable."""
    r = annual_rate / 12.0
    if monthly_payment <= principal * r:
        return None  # won't amortise
    # N = log(Pmt/(Pmt-r*PV)) / log(1+r)
    n_months = math.log(monthly_payment / (monthly_payment - r * principal)) / math.log(1 + r)
    return n_months / 12.0

def safe_float(x: str) -> float | None:
    try:
        return float(re.sub(r"[^\d.\-]", "", x))
    except Exception:
        return None

# -----------------------------
# Data adapters (plug & play)
# -----------------------------
def sqm_vacancy_series(postcode: str) -> pd.DataFrame:
    """
    Fetch vacancy chart page and attempt to parse time series.
    NOTE: SQM may change page structure; treat this as best-effort.
    Source pages show the postcode endpoint exists. (e.g., graph_vacancy.php?postcode=3500&t=1)
    """
    url = f"https://sqmresearch.com.au/graph_vacancy.php?postcode={postcode}&t=1"
    html = requests.get(url, timeout=20).text
    # Best-effort parse: look for embedded JS arrays
    # Often pages contain something like: data.addRows([[new Date(YYYY,MM,DD), value], ...])
    rows = []
    for m in re.finditer(r"new Date\((\d{4}),\s*(\d{1,2}),\s*(\d{1,2})\)\s*,\s*([0-9.]+)", html):
        y, mo, d, v = m.groups()
        rows.append((f"{int(y)}-{int(mo)+1:02d}-{int(d):02d}", float(v)))  # mo may be 0-indexed
    df = pd.DataFrame(rows, columns=["date", "vacancy_rate"]).drop_duplicates()
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date")
    return df

def sqm_stock_on_market_series(postcode: str) -> pd.DataFrame:
    """
    Fetch total listings (stock on market) series for a postcode.
    Endpoint exists. Example: total-property-listings.php?postcode=4210&t=1
    """
    url = f"https://sqmresearch.com.au/total-property-listings.php?postcode={postcode}&t=1"
    html = requests.get(url, timeout=20).text
    rows = []
    for m in re.finditer(r"new Date\((\d{4}),\s*(\d{1,2}),\s*(\d{1,2})\)\s*,\s*([0-9.]+)", html):
        y, mo, d, v = m.groups()
        rows.append((f"{int(y)}-{int(mo)+1:02d}-{int(d):02d}", float(v)))
    df = pd.DataFrame(rows, columns=["date", "total_listings"]).drop_duplicates()
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date")
    return df

def classify_growth_cycle(series: pd.Series) -> str:
    """
    Simple regime classifier using momentum:
    - compute 3m and 12m % change; compare sign/magnitude.
    """
    if series is None or len(series) < 13:
        return "Unknown (need ≥ 13 data points)"
    s = series.dropna()
    if len(s) < 13:
        return "Unknown (insufficient clean points)"
    last = s.iloc[-1]
    m3 = (last / s.iloc[-4] - 1.0) * 100
    m12 = (last / s.iloc[-13] - 1.0) * 100
    if m3 > 0 and m12 > 0 and m3 >= m12 * 0.6:
        return f"Upswing (3m {m3:.1f}%, 12m {m12:.1f}%)"
    if m12 > 0 and m3 < 0:
        return f"Slowdown (3m {m3:.1f}%, 12m {m12:.1f}%)"
    if m12 < 0 and m3 > 0:
        return f"Recovery (3m {m3:.1f}%, 12m {m12:.1f}%)"
    if m3 < 0 and m12 < 0:
        return f"Downturn (3m {m3:.1f}%, 12m {m12:.1f}%)"
    return f"Mixed (3m {m3:.1f}%, 12m {m12:.1f}%)"

# -----------------------------
# UI
# -----------------------------
st.title("AU Suburb Analyzer")

with st.sidebar:
    st.header("Input")
    suburb = st.text_input("Suburb (optional if postcode provided)", placeholder="e.g., Portland")
    state = st.text_input("State (optional)", placeholder="e.g., VIC")
    postcode = st.text_input("Postcode", placeholder="e.g., 3305")

    st.divider()
    st.subheader("Affordability assumptions")
    median_price = st.number_input("Median dwelling value ($)", min_value=0, value=650000, step=5000)
    median_household_income = st.number_input("Median household income (annual $)", min_value=0, value=95000, step=1000)
    interest_rate = st.number_input("Interest rate (annual %)", min_value=0.0, value=6.5, step=0.1) / 100.0
    deposit_pct = st.number_input("Deposit (%)", min_value=0.0, max_value=100.0, value=20.0, step=1.0) / 100.0
    affordability_pct = st.number_input("Max repayment as % of income", min_value=0.0, max_value=100.0, value=30.0, step=1.0) / 100.0

    st.divider()
    uploaded_growth = st.file_uploader("Optional: upload CSV of suburb value index (date,value)", type=["csv"])

if not postcode:
    st.warning("Enter a postcode to fetch SQM vacancy + listings. (Suburb/state mapping can be added later.)")
    st.stop()

# Affordability calc
loan = median_price * (1 - deposit_pct)
monthly_income = median_household_income / 12.0
max_payment = monthly_income * affordability_pct
years = amortisation_years(loan, interest_rate, max_payment)

colA, colB, colC = st.columns(3)
with colA:
    st.metric("Loan (assumed)", f"${loan:,.0f}")
with colB:
    st.metric("Max monthly repayment (30%)", f"${max_payment:,.0f}")
with colC:
    st.metric("Affordability (years)", "Not serviceable" if years is None else f"{years:.1f}")

st.divider()

# Vacancy + Stock on market
c1, c2 = st.columns(2)
with c1:
    st.subheader("Vacancy rate (SQM)")
    try:
        vac = sqm_vacancy_series(postcode)
        if vac.empty:
            st.info("Couldn’t parse a vacancy time series (site format may have changed).")
        else:
            st.line_chart(vac.set_index("date")["vacancy_rate"])
            st.write(vac.tail(6))
    except Exception as e:
        st.error(f"Vacancy fetch failed: {e}")

with c2:
    st.subheader("Stock on market (Total listings, SQM)")
    try:
        som = sqm_stock_on_market_series(postcode)
        if som.empty:
            st.info("Couldn’t parse a listings time series (site format may have changed).")
        else:
            st.line_chart(som.set_index("date")["total_listings"])
            # simple trend: last 3 vs prior 3
            if len(som) >= 6:
                recent = som["total_listings"].tail(3).mean()
                prior = som["total_listings"].iloc[-6:-3].mean()
                st.caption(f"3-mo avg vs prior 3-mo: {((recent/prior)-1)*100:.1f}%")
            st.write(som.tail(6))
    except Exception as e:
        st.error(f"Listings fetch failed: {e}")

st.divider()

# Growth cycle (optional uploaded series)
st.subheader("Growth cycle (from your uploaded value series)")
if uploaded_growth is None:
    st.info("Upload a CSV with columns: date,value (monthly) to classify the growth cycle.")
else:
    df = pd.read_csv(uploaded_growth)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date")
    st.line_chart(df.set_index("date")["value"])
    cycle = classify_growth_cycle(df["value"])
    st.success(cycle)

st.divider()

# Tenure + DA approvals placeholders
st.subheader("Renter : owner ratio + DA approvals")
st.write(
    "Hook this to ABS Census tenure (owned outright + mortgage vs rented) and to your council/state planning portal.\n\n"
    "- ABS tenure categories are standardised (owned outright / owned with mortgage / rented...).\n"
    "- DA/permit data varies by council; some publish monthly status PDFs or registers."
)
