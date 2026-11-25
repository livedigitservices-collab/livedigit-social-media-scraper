"""Streamlit entry point."""

import hmac
import os
import sqlite3

import pandas as pd
import streamlit as st

from livedigit.discovery import CATEGORIES, LOCATIONS, SOCIAL
from livedigit.export import csv_bytes, excel_bytes, table
from livedigit.search import Search, SearchError, Settings, run_search
from livedigit.store import Store

st.set_page_config(page_title="LiveDigit Social Media Lead Finder", page_icon="📍", layout="wide")
st.title("LiveDigit Social Media Lead Finder")
st.caption("Find local business profiles and publicly published contact details in Andhra Pradesh and Telangana.")

password = os.getenv("DASHBOARD_PASSWORD", "")
if os.getenv("RAILWAY_ENVIRONMENT_ID") and not password:
    st.error("Set DASHBOARD_PASSWORD in Railway before exposing this service.")
    st.stop()
if password and not st.session_state.get("dashboard_authenticated"):
    with st.form("dashboard_access"):
        supplied = st.text_input("Dashboard password", type="password")
        unlocked = st.form_submit_button("Open dashboard", type="primary")
        if unlocked:
            if hmac.compare_digest(supplied.encode(), password.encode()):
                st.session_state["dashboard_authenticated"] = True
                st.rerun()
            st.error("Incorrect password.")
    st.stop()

try:
    settings = Settings.load()
    store = Store(settings.db_path)
except (ValueError, OSError, sqlite3.Error):
    st.error("Could not initialize the dashboard. Check the configured limits and SQLite path.")
    st.stop()

with st.sidebar:
    st.header("Search filters")
    state = st.selectbox("State", list(LOCATIONS))
    city = st.selectbox("City", [*LOCATIONS[state], "Custom location"])
    custom_city = st.text_input("Custom city or town", max_chars=80) if city == "Custom location" else city
    with st.form("search"):
        category = st.selectbox("Business category", list(CATEGORIES))
        platforms = st.multiselect("Platforms", list(SOCIAL), default=list(SOCIAL))
        languages = st.multiselect("Languages", ["English", "Telugu"], default=["English", "Telugu"])
        keyword = st.text_input("Custom keyword", max_chars=100)
        pages = st.slider("Pages per query", 1, 5, 1)
        website_lookup = st.checkbox("Look up official websites", help="A website lookup uses one additional Serper request per eligible profile.")
        st.caption("Each platform, language, and page combination uses one Serper request.")
        submitted = st.form_submit_button("Find businesses", type="primary", disabled=not settings.key)
    if not settings.key:
        st.info("Add SERPER_API_KEY in your environment to enable searches.")

if submitted:
    bar = st.progress(0, text="Preparing search")

    def progress(message, done, total):
        bar.progress(min(done / total, 1.0), text=message)

    try:
        search_id, status = run_search(
            Search(state, custom_city.strip(), category, tuple(platforms), tuple(languages),
                   keyword.strip(), pages, website_lookup),
            settings, store, progress,
        )
        st.session_state["lead_view"] = search_id
        if status == "Completed":
            st.success("Search complete.")
        else:
            st.warning(status)
    except SearchError as exc:
        bar.empty()
        st.warning(str(exc))

metrics = (
    "Businesses discovered", "With contact numbers", "Instagram profiles",
    "Facebook pages", "Duplicate matches removed", "API credits attempted",
)
values = store.metrics()
for labels, counts in ((metrics[:3], values[:3]), (metrics[3:], values[3:])):
    for column, label, value in zip(st.columns(3), labels, counts):
        column.metric(label, f"{value:,}")
profiles_today, credits_today = store.today()
st.caption(f"Today (India time) · {profiles_today:,}/{settings.daily_target:,} new profiles · "
           f"{credits_today:,}/{settings.credit_limit:,} API requests. Provider billing may differ.")

results_tab, history_tab = st.tabs(["Business leads", "Search history"])
history = store.history()
with results_tab:
    options = {0: "All searches", **{row["id"]: f'{row["description"]} · {row["started"][:16].replace("T", " ")} · #{row["id"]}'
                                    for row in history}}
    if st.session_state.get("lead_view") not in options:
        st.session_state["lead_view"] = 0
    search_id = st.selectbox("Results from", list(options), format_func=options.get, key="lead_view")
    contacts_only = st.toggle("Only businesses with a contact number")
    frame = table(store.rows(search_id or None, contacts_only))
    if frame.empty:
        st.info("No businesses match this view.")
    else:
        st.dataframe(
            frame, hide_index=True, width="stretch",
            column_config={label: st.column_config.LinkColumn(label) for label in
                           ("Instagram URL", "Facebook URL", "Website", "Contact source URL", "WhatsApp source URL", "Email source URL")},
        )
        excel, csv = st.columns(2)
        with excel:
            st.download_button("Download Excel", lambda: excel_bytes(frame), "livedigit-leads.xlsx",
                               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", type="primary")
        with csv:
            st.download_button("Download CSV", lambda: csv_bytes(frame), "livedigit-leads.csv", "text/csv")
    st.caption("Location and category are based on search evidence. A valid mobile format does not verify ownership.")

with history_tab:
    if history:
        columns = {
            "id": "Search ID", "started": "Started (India time)", "description": "Filters", "status": "Status",
            "matches": "Profiles matched", "duplicates": "Duplicate matches", "credits": "API requests",
        }
        st.dataframe(pd.DataFrame(history)[list(columns)].rename(columns=columns), hide_index=True, width="stretch")
    else:
        st.info("Completed and partial searches will appear here.")
