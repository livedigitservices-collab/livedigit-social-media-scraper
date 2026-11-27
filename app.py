"""Streamlit entry point."""

import hmac
import os
import sqlite3

import pandas as pd
import streamlit as st

from livedigit.discovery import CATEGORIES, LOCATIONS
from livedigit.export import csv_bytes, excel_bytes, table
from livedigit.search import LANGUAGES, MODES, Search, SearchError, Settings, run_search
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

def clear_filters():
    for key in ("search_state", "search_city", "search_custom_city", "search_category", "search_mode",
                "search_languages", "search_keyword", "search_variants", "search_pages", "search_lookup",
                "search_lookup_limit", "bulk_search", "bulk_variants", "bulk_pages", "contacts_only",
                "lead_view", "results_source"):
        st.session_state.pop(key, None)


with st.sidebar:
    st.header("Search filters")
    st.button("Clear filters", on_click=clear_filters, key="clear_filters")
    state = st.selectbox("State", list(LOCATIONS), key="search_state")
    city = st.selectbox("City", [*LOCATIONS[state], "Custom location"], key="search_city")
    custom_city = (st.text_input("Custom city or town", max_chars=80, key="search_custom_city")
                   if city == "Custom location" else city)
    category = st.selectbox("Business category", list(CATEGORIES), key="search_category")
    mode = st.selectbox("Search mode", list(MODES), index=4, key="search_mode")
    languages = st.multiselect("Languages", LANGUAGES, default=LANGUAGES, key="search_languages")
    keyword = st.text_input("Custom keyword", max_chars=100, key="search_keyword")
    bulk = st.checkbox("Bulk search (up to 1,000 profiles)", key="bulk_search")
    variants = st.slider("Query variants per source and language", 1, 8 if bulk else 3, 8 if bulk else 2,
                         key="bulk_variants" if bulk else "search_variants")
    pages = st.slider("Pages per query", 1, 10 if bulk else 5, 10 if bulk else 1,
                      key="bulk_pages" if bulk else "search_pages")
    lookup = st.checkbox("Look up missing official websites", key="search_lookup")
    lookup_limit = (st.slider("Maximum additional website lookups", 1, 20, 5, key="search_lookup_limit")
                    if lookup else 0)
    spec = Search(state, custom_city.strip(), category, MODES[mode], tuple(languages),
                  keyword.strip(), pages, lookup, variants, lookup_limit)
    remaining = max(0, settings.credit_limit - store.today()[1])
    ready = bool(spec.city and spec.languages)
    if ready:
        estimated = spec.estimated_requests()
        st.caption(f"Estimated upper bound {estimated} request credits, with {remaining} remaining today. "
                   "Empty or repeated pages can reduce usage.")
        if bulk:
            st.caption("Bulk search can take several minutes. It stops at 1,000 accepted profiles, "
                       "the daily limit, or the end of available results.")
        with st.expander("Preview search queries"):
            st.dataframe(
                [{"Source": platform, "Language": language, "Query": query}
                 for platform, language, query in spec.queries()],
                hide_index=True, width="stretch",
            )
    submitted = st.button("Find businesses", type="primary", disabled=not (settings.key and ready and remaining), key="run_search")
    if not settings.key:
        st.info("Add SERPER_API_KEY in your environment to enable searches.")

if submitted:
    bar = st.progress(0, text="Preparing search")

    def progress(message, done, total):
        bar.progress(min(done / total, 1.0), text=message)

    try:
        search_id, status = run_search(spec, settings, store, progress)
        st.session_state["diagnostic_view"] = search_id
        st.session_state["lead_view"] = search_id
        st.session_state["contacts_only"] = False
        st.session_state["results_source"] = "All sources"
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

results_tab, diagnostics_tab, history_tab = st.tabs(["Business leads", "Search diagnostics", "Search history"])
history = store.history()
with results_tab:
    options = {0: "All searches", **{row["id"]: f'{row["description"]} · {row["started"][:16].replace("T", " ")} · #{row["id"]}'
                                    for row in history}}
    if st.session_state.get("lead_view") not in options:
        st.session_state["lead_view"] = 0
    search_id = st.selectbox("Results from", list(options), format_func=options.get, key="lead_view")
    source_fields = {"Instagram": "instagram", "Facebook": "facebook", "Google Maps": "maps"}
    source = st.selectbox("Filter by profile or listing", ["All sources", *source_fields], key="results_source")
    contacts_only = st.toggle("Only businesses with a contact number", key="contacts_only")
    rows = store.rows(search_id or None, contacts_only)
    if source in source_fields:
        rows = [row for row in rows if row[source_fields[source]]]
    st.caption(f"{len(rows):,} businesses in this view")
    frame = table(rows)
    if frame.empty:
        st.info("No businesses match this view.")
    else:
        st.dataframe(
            frame, hide_index=True, width="stretch",
            column_config={label: st.column_config.LinkColumn(label) for label in
                           ("Instagram URL", "Facebook URL", "Google Maps listing URL", "Website",
                            "Contact source URL", "WhatsApp source URL", "Email source URL", "Landline source URL")},
        )
        excel, csv = st.columns(2)
        with excel:
            st.download_button("Download Excel", lambda: excel_bytes(frame), "livedigit-leads.xlsx",
                               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", type="primary")
        with csv:
            st.download_button("Download CSV", lambda: csv_bytes(frame), "livedigit-leads.csv", "text/csv")
    st.caption("Location and category are based on search evidence. Valid number formats do not verify ownership.")

with diagnostics_tab:
    if not history:
        st.info("Run a search to see discovery and contact collection statistics.")
    else:
        by_id = {row["id"]: row for row in history}
        if st.session_state.get("diagnostic_view") not in by_id:
            st.session_state["diagnostic_view"] = next(iter(by_id))
        selected = st.selectbox("Search to inspect", list(by_id), key="diagnostic_view",
                                format_func=lambda value: f"#{value} · {by_id[value]['description']}")
        stats = store.diagnostics(selected)
        businesses = store.rows(selected)
        contacts = sum(bool(row["mobile"] or row["whatsapp"] or row["landline"]) for row in businesses)
        st.caption(by_id[selected]["status"])
        for column, (label, value) in zip(st.columns(4), (
            ("Raw search results", stats.get("raw", "—")),
            ("Accepted profiles/listings", stats.get("accepted", "—")),
            ("Rejected results", sum(count for label, count in stats.items() if label.startswith("Rejected:")) if stats else "—"),
            ("API errors", stats.get("api_errors", "—")),
        )):
            column.metric(label, value)
        for column, (label, value) in zip(st.columns(3), (
            ("Businesses with contact numbers", contacts),
            ("API credits attempted", by_id[selected]["credits"]),
            ("Contact collection rate", f"{contacts / len(businesses):.1%}" if businesses else "—"),
        )):
            column.metric(label, value)
        reasons = [{"Reason": label.removeprefix("Rejected: "), "Results": count}
                   for label, count in sorted(stats.items()) if label.startswith("Rejected:")]
        if reasons:
            st.dataframe(reasons, hide_index=True, width="stretch")
        if stats:
            st.caption("Raw results cover discovery pages. The contact rate uses deduplicated businesses "
                       "currently linked to the search. Request counts estimate credits.")
        else:
            st.caption("Raw results and rejection reasons were not recorded for Phase 1 searches.")

with history_tab:
    if history:
        columns = {
            "id": "Search ID", "started": "Started (India time)", "description": "Filters", "status": "Status",
            "matches": "Profiles matched", "duplicates": "Duplicate matches", "credits": "API requests",
        }
        st.dataframe(pd.DataFrame(history)[list(columns)].rename(columns=columns), hide_index=True, width="stretch")
    else:
        st.info("Completed and partial searches will appear here.")
