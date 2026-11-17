"""AgriShare Africa — MVP (Streamlit).

Prototype platform for farm discovery, viability screening, field updates,
non-binding investment interest and a simple agricultural marketplace.
"""
from __future__ import annotations

import hmac
import logging
import os
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import folium
import pandas as pd
import plotly.express as px
import streamlit as st
from streamlit_folium import st_folium

import agrishare_utils as U

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("agrishare")

st.set_page_config(
    page_title="AgriShare Africa",
    page_icon="🌱",
    layout="wide",
    initial_sidebar_state="expanded",
    menu_items={"Get help": None, "Report a bug": None,
                "About": "AgriShare Africa — prototype for validation and demonstration."},
)

# ----------------------------- Configuration -------------------------------
APP_DIR = Path(__file__).parent
SEED_DEMO_DATA = os.getenv("SEED_DEMO_DATA", "1").lower() in ("1", "true", "yes")
CONTACT_EMAIL = os.getenv("CONTACT_EMAIL", "")

STATUSES = ["Pending", "Under review", "Verified", "Rejected", "Demo"]
CROPS = ["Maize", "Rice", "Cocoa", "Cashew", "Cassava", "Plantain", "Vegetables", "Livestock", "Mixed", "Other"]
LAND_ACCESS = ["Owned (documents supplied)", "Lease / documented access", "Customary / family land",
               "Permit / concession", "Documentation pending", "Other"]
UPDATE_CATEGORIES = ["Planting", "Crop growth", "Irrigation", "Pest / disease", "Harvest",
                     "Input purchase", "Inspection", "Other"]
LISTING_TYPES = ["Input / service", "Produce for sale"]
LISTING_CATEGORIES = ["Seeds", "Fertiliser", "Equipment", "Irrigation", "Crop produce", "Livestock",
                      "Transport", "Other"]
MAX_LIST = 50  # cap on expanders rendered per page, to keep pages fast
ESRI_TILES = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
ESRI_ATTR = "Tiles © Esri — Source: Esri, Maxar, Earthstar Geographics, and the GIS User Community"


def _resolve_db_path() -> Path:
    """AGRISHARE_DB wins; otherwise /data (persistent volume) if writable; otherwise the app dir;
    finally the system temp dir, so the app still starts on read-only or restricted filesystems."""
    env = os.getenv("AGRISHARE_DB")
    candidates = [Path(env)] if env else []
    candidates += [Path("/data/agrishare.db"), APP_DIR / "agrishare.db",
                   Path(tempfile.gettempdir()) / "agrishare.db"]
    for path in candidates:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if os.access(path.parent, os.W_OK):
                return path
        except OSError:
            continue
    return candidates[-1]


DB_PATH = _resolve_db_path()


# ----------------------------- Database ------------------------------------
@contextmanager
def db():
    """Open a short-lived connection; commit on success, roll back on error, always close."""
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


SCHEMA = """
CREATE TABLE IF NOT EXISTS farms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL, farmer TEXT NOT NULL, country TEXT NOT NULL, region TEXT NOT NULL,
    crop TEXT NOT NULL, acres REAL NOT NULL, latitude REAL NOT NULL, longitude REAL NOT NULL,
    land_access TEXT NOT NULL,
    water_score INTEGER NOT NULL, soil_score INTEGER NOT NULL, market_score INTEGER NOT NULL,
    climate_score INTEGER NOT NULL, operations_score INTEGER NOT NULL,
    capital_needed REAL NOT NULL, expected_revenue REAL NOT NULL, estimated_cost REAL NOT NULL,
    description TEXT,
    verification TEXT NOT NULL DEFAULT 'Pending',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS updates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    farm_id INTEGER NOT NULL, update_date TEXT NOT NULL, category TEXT NOT NULL,
    note TEXT NOT NULL, image_url TEXT,
    FOREIGN KEY(farm_id) REFERENCES farms(id)
);
CREATE TABLE IF NOT EXISTS listings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_type TEXT NOT NULL, title TEXT NOT NULL, category TEXT NOT NULL, location TEXT NOT NULL,
    quantity TEXT NOT NULL, price TEXT NOT NULL, contact TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS interests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    farm_id INTEGER NOT NULL, investor_name TEXT NOT NULL, investor_email TEXT NOT NULL,
    amount REAL NOT NULL, note TEXT,
    status TEXT NOT NULL DEFAULT 'Expression of interest - not an investment',
    created_at TEXT NOT NULL,
    FOREIGN KEY(farm_id) REFERENCES farms(id)
);
"""
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_updates_farm ON updates(farm_id, update_date);
CREATE INDEX IF NOT EXISTS idx_interests_farm ON interests(farm_id);
CREATE INDEX IF NOT EXISTS idx_farms_verification ON farms(verification);
"""
SAMPLES = [
    ("Akwapim Maize Project", "Kwame Asare", "Ghana", "Eastern Region", "Maize", 18, 6.10, -0.27,
     "Lease / documented access", 75, 78, 82, 70, 80, 12000, 19500, 12500,
     "Illustrative farm profile. Data requires independent verification."),
    ("Bono Cashew Farm", "Ama Mensah", "Ghana", "Bono East", "Cashew", 32, 7.75, -1.05,
     "Family land / documentation pending", 65, 80, 72, 74, 76, 18000, 28000, 19000,
     "Illustrative cashew enterprise profile."),
    ("Volta Rice Growers", "Yaw Boateng", "Ghana", "Volta Region", "Rice", 25, 6.55, 0.45,
     "Lease / documented access", 88, 76, 78, 77, 83, 22000, 35000, 24500,
     "Illustrative irrigated rice project."),
]


@st.cache_resource(show_spinner=False)
def init_db() -> bool:
    """Create tables, apply lightweight migrations and (optionally) seed demo data. Runs once per process."""
    with db() as con:
        try:
            con.execute("PRAGMA journal_mode=WAL")  # better concurrent read/write behaviour
        except sqlite3.DatabaseError:
            log.warning("WAL mode unavailable on this filesystem; using default journal.")
        con.executescript(SCHEMA)
        cols = {r["name"] for r in con.execute("PRAGMA table_info(farms)")}
        if "currency" not in cols:
            con.execute("ALTER TABLE farms ADD COLUMN currency TEXT NOT NULL DEFAULT 'EUR'")
        if "update_key_hash" not in cols:
            con.execute("ALTER TABLE farms ADD COLUMN update_key_hash TEXT")
        con.executescript(INDEXES)
        if SEED_DEMO_DATA and con.execute("SELECT COUNT(*) FROM farms").fetchone()[0] == 0:
            con.executemany(
                """INSERT INTO farms (name,farmer,country,region,crop,acres,latitude,longitude,land_access,
                   water_score,soil_score,market_score,climate_score,operations_score,capital_needed,
                   expected_revenue,estimated_cost,description,verification,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'Demo',?)""",
                [s + (now_iso(),) for s in SAMPLES],
            )
    log.info("Database ready at %s", DB_PATH)
    return True


init_db()

SCORE_COLS = ["water_score", "soil_score", "market_score", "climate_score", "operations_score"]


@st.cache_data(ttl=60, show_spinner=False)
def load_farms(include_rejected: bool = False) -> pd.DataFrame:
    query = "SELECT * FROM farms"
    if not include_rejected:
        query += " WHERE verification != 'Rejected'"
    query += " ORDER BY created_at DESC, id DESC"
    with db() as con:
        df = pd.read_sql_query(query, con)
    df = df.drop(columns=["update_key_hash"])  # never expose the hash in the UI or exports
    if not df.empty:
        # Transparent prototype score (equal weights) — not an investment rating or yield prediction.
        df["farm_score"] = df[SCORE_COLS].mean(axis=1).round().astype(int)
        df["estimated_margin"] = df["expected_revenue"] - df["estimated_cost"]
    return df


def invalidate() -> None:
    st.cache_data.clear()


# ----------------------------- UI helpers ----------------------------------
e = U.md_escape  # escape user text before putting it in Markdown


def section_header(title: str, subtitle: str | None = None) -> None:
    st.title(title)
    if subtitle:
        st.caption(subtitle)


def flash(message: str, kind: str = "success") -> None:
    st.session_state["_flash"] = (kind, message)


def show_flash() -> None:
    item = st.session_state.pop("_flash", None)
    if item:
        getattr(st, item[0])(item[1])


def form_key(form: str, name: str) -> str:
    """Widget keys carry a version number; bumping it after a successful submit blanks the form,
    while a failed validation leaves what the user typed untouched."""
    return f"{form}_{name}_{st.session_state.get(f'ver_{form}', 0)}"


def reset_form(form: str) -> None:
    st.session_state[f"ver_{form}"] = st.session_state.get(f"ver_{form}", 0) + 1


def throttled(action: str, seconds: int) -> bool:
    """Per-session cooldown against accidental double submits and casual spam."""
    wait = seconds - (time.time() - st.session_state.get(f"last_{action}", 0))
    if wait > 0:
        st.error(f"Please wait {int(wait) + 1} seconds before submitting again.")
        return True
    return False


def mark_submitted(action: str) -> None:
    st.session_state[f"last_{action}"] = time.time()


def farm_picker(df: pd.DataFrame, label: str, key: str) -> pd.Series:
    """Select a farm by id (names are not unique)."""
    labels = {int(r["id"]): f"{r['name']} — {r['region']}, {r['country']}" for _, r in df.iterrows()}
    counts = pd.Series(list(labels.values())).value_counts()
    labels = {i: (f"{t} · #{i}" if counts[t] > 1 else t) for i, t in labels.items()}  # keep labels unique
    chosen = st.selectbox(label, list(labels), format_func=lambda i: labels[i], key=key)
    return df[df["id"] == chosen].iloc[0]


def capital_summary(df: pd.DataFrame) -> str:
    """Sum capital per currency — never add different currencies together."""
    if df.empty:
        return "—"
    totals = df.groupby("currency")["capital_needed"].sum()
    return " + ".join(U.money(v, c) for c, v in totals.items())


def build_map(df: pd.DataFrame, circles: bool = False) -> folium.Map:
    m = folium.Map(location=[2.0, 20.0], zoom_start=3, tiles=None, control_scale=True)  # centred on Africa
    folium.TileLayer("OpenStreetMap", name="Street map").add_to(m)
    folium.TileLayer(tiles=ESRI_TILES, attr=ESRI_ATTR, name="Satellite imagery",
                     overlay=False, control=True).add_to(m)
    for _, r in df.iterrows():
        H = U.html_escape  # every user-controlled value is escaped before it reaches HTML/JS
        popup = folium.Popup(
            f"<b>{H(r['name'])}</b><br>{H(r['crop'])} • {r['acres']:,.1f} acres"
            f"<br>FarmScore: {r['farm_score']}/100", max_width=260)
        loc = [float(r["latitude"]), float(r["longitude"])]
        if circles:
            folium.CircleMarker(loc, radius=8, popup=popup, tooltip=H(r["name"]), fill=True).add_to(m)
        else:
            folium.Marker(loc, popup=popup, tooltip=H(r["name"])).add_to(m)
    if len(df) == 1:
        m.location, m.options["zoom"] = [float(df.iloc[0]["latitude"]), float(df.iloc[0]["longitude"])], 8
    elif len(df) > 1:
        m.fit_bounds([[df["latitude"].min(), df["longitude"].min()],
                      [df["latitude"].max(), df["longitude"].max()]], padding=(30, 30), max_zoom=10)
    folium.LayerControl().add_to(m)
    return m


def get_admin_password() -> str:
    pw = os.getenv("ADMIN_PASSWORD", "")
    if not pw:
        try:
            pw = str(st.secrets.get("ADMIN_PASSWORD", ""))
        except Exception:  # no secrets file configured
            pw = ""
    return pw


def is_admin() -> bool:
    return bool(st.session_state.get("is_admin"))


# ----------------------------- Pages ---------------------------------------
def page_overview(farms: pd.DataFrame) -> None:
    section_header("Agriculture, made visible.",
                   "A digital platform connecting farmers, agricultural markets and potential investors.")
    show_flash()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Farms listed", len(farms))
    c2.metric("Countries", farms["country"].nunique() if not farms.empty else 0)
    c3.metric("Total mapped area", f"{farms['acres'].sum():,.1f} acres" if not farms.empty else "0 acres")
    c4.metric("Investment sought", capital_summary(farms))
    st.warning("Demonstration environment. Farm information, scores and financial projections must be "
               "verified before being used for investment decisions.")
    left, right = st.columns([1.4, 1])
    with left:
        st.subheader("Farm geography")
        if farms.empty:
            st.info("No farms registered yet.")
        else:
            st_folium(build_map(farms), height=430, use_container_width=True,
                      returned_objects=[], key="map_overview")
    with right:
        st.subheader("Farm portfolio by crop")
        if farms.empty:
            st.info("Register the first farm to populate the dashboard.")
        else:
            crop_df = farms.groupby("crop", as_index=False)["acres"].sum()
            st.plotly_chart(px.pie(crop_df, names="crop", values="acres", hole=0.45), width="stretch")
            st.subheader("Recently listed farms")
            for _, r in farms.head(4).iterrows():
                st.markdown(f"**{e(r['name'])}** · {e(r['region'])}, {e(r['country'])}")
                st.caption(f"{e(r['crop'])} · {r['acres']:,.1f} acres · FarmScore {r['farm_score']}/100")


def page_explore(farms: pd.DataFrame) -> None:
    section_header("Explore farms", "Discover registered farm projects and inspect their mapped locations.")
    if farms.empty:
        st.info("No farms available.")
        return
    a, b, c = st.columns(3)
    crop_filter = a.selectbox("Crop", ["All"] + sorted(farms["crop"].unique().tolist()))
    country_filter = b.selectbox("Country", ["All"] + sorted(farms["country"].unique().tolist()))
    min_score = c.slider("Minimum FarmScore", 0, 100, 0)
    view = farms.copy()
    if crop_filter != "All":
        view = view[view.crop == crop_filter]
    if country_filter != "All":
        view = view[view.country == country_filter]
    view = view[view.farm_score >= min_score]
    st.caption(f"{len(view)} matching farm(s)")
    st_folium(build_map(view, circles=True), height=480, use_container_width=True,
              returned_objects=[], key="map_explore")
    if len(view) > MAX_LIST:
        st.caption(f"Showing the first {MAX_LIST} farms below — use the filters to narrow the list.")
    for _, r in view.head(MAX_LIST).iterrows():
        with st.expander(f"{r['name']} — {r['crop']} | {r['region']}, {r['country']}"):
            x, y, z = st.columns(3)
            x.metric("Area", f"{r['acres']:,.1f} acres")
            y.metric("FarmScore", f"{r['farm_score']}/100")
            z.metric("Capital sought", U.money(r["capital_needed"], r["currency"]))
            st.markdown(e(r["description"]))
            st.caption(f"Farmer: {e(r['farmer'])} · Land access: {e(r['land_access'])} · "
                       f"Verification: {e(r['verification'])}")
            st.caption(f"Coordinates: {r['latitude']:.5f}, {r['longitude']:.5f}")
            st.info("A map pin is not proof of land title, boundaries, crop condition or commercial viability. "
                    "Independent checks are required.")


def page_register(farms: pd.DataFrame) -> None:
    section_header("Register a farm", "Submit a farm profile. New registrations are marked pending verification.")
    show_flash()
    pending = st.session_state.get("new_farm_code")
    if pending:
        st.success(f"“{pending[0]}” was submitted and is pending verification.")
        st.warning("**Save your farm update code now — it is shown only once.** You need it to post field "
                   "updates for this farm. We store only a one-way hash and cannot recover it.")
        st.code(pending[1], language=None)
        if st.button("I have saved my update code", key="dismiss_code"):
            del st.session_state["new_farm_code"]
            st.rerun()
        st.divider()

    k = lambda n: form_key("register", n)  # noqa: E731
    with st.form("farm_form"):
        st.subheader("Farm identity")
        col1, col2 = st.columns(2)
        name = col1.text_input("Farm / project name *", max_chars=120, key=k("name"))
        farmer = col2.text_input("Farmer or enterprise name *", max_chars=120, key=k("farmer"))
        country = col1.text_input("Country *", max_chars=80, placeholder="e.g. Ghana", key=k("country"))
        region = col2.text_input("Region / district *", max_chars=120, key=k("region"))
        crop = col1.selectbox("Main crop", CROPS, key=k("crop"))
        acres = col2.number_input("Farm size (acres)", min_value=0.1, max_value=1_000_000.0, value=5.0,
                                  step=0.5, key=k("acres"))
        st.subheader("Location")
        col1, col2 = st.columns(2)
        lat = col1.number_input("Latitude *", min_value=-90.0, max_value=90.0, value=None, format="%.6f",
                                placeholder="e.g. 6.100000", key=k("lat"))
        lon = col2.number_input("Longitude *", min_value=-180.0, max_value=180.0, value=None, format="%.6f",
                                placeholder="e.g. -0.270000", key=k("lon"))
        land_access = st.selectbox("Land access / tenure status", LAND_ACCESS, key=k("land"))
        st.caption("Use a GPS point inside the farm. The point is shown publicly on the map — if you prefer, "
                   "use a nearby approximate point. Boundary polygons and documents belong in a later "
                   "verification workflow.")
        st.subheader("Initial evidence scores")
        st.caption("Enter provisional scores from 0 to 100. These are self-reported until verified.")
        s1, s2, s3 = st.columns(3)
        water = s1.slider("Water access", 0, 100, 50, key=k("water"))
        soil = s2.slider("Soil suitability", 0, 100, 50, key=k("soil"))
        market = s3.slider("Market access", 0, 100, 50, key=k("market"))
        s1, s2 = st.columns(2)
        climate = s1.slider("Climate resilience", 0, 100, 50, key=k("climate"))
        operations = s2.slider("Operational capacity", 0, 100, 50, key=k("ops"))
        st.subheader("Project economics (estimates)")
        s1, s2, s3, s4 = st.columns(4)
        currency = s1.selectbox("Currency", U.CURRENCIES, key=k("currency"))
        capital = s2.number_input("Capital required", min_value=0.0, max_value=1e10, value=5000.0,
                                  step=500.0, key=k("capital"))
        revenue = s3.number_input("Expected revenue", min_value=0.0, max_value=1e10, value=8000.0,
                                  step=500.0, key=k("revenue"))
        costs = s4.number_input("Estimated costs", min_value=0.0, max_value=1e10, value=5000.0,
                                step=500.0, key=k("costs"))
        description = st.text_area("Farm description, production history and intended use of funds",
                                   max_chars=2000, key=k("desc"))
        consent = st.checkbox("I confirm this information is accurate to the best of my knowledge and I agree "
                              "that the farm name, farmer name, region, location and description will be "
                              "publicly visible.", key=k("consent"))
        submitted = st.form_submit_button("Submit farm for review", type="primary")

    if not submitted:
        return
    name, farmer, country, region = (U.clean_text(v, 120) for v in (name, farmer, country, region))
    description = U.clean_text(description, 2000)
    if not all([name, farmer, country, region]):
        st.error("Please complete the farm name, farmer / enterprise, country and region.")
    elif not U.valid_coordinates(lat, lon):
        st.error("Please enter a valid latitude and longitude (they cannot be empty or exactly 0, 0).")
    elif capital <= 0:
        st.error("Capital required must be greater than zero.")
    elif not consent:
        st.error("Please confirm the accuracy and public-visibility statement.")
    elif throttled("register", 60):
        pass
    else:
        with db() as con:
            duplicate = con.execute(
                "SELECT 1 FROM farms WHERE lower(name)=lower(?) AND lower(farmer)=lower(?)",
                (name, farmer)).fetchone()
            if duplicate:
                st.error("A farm with this name and farmer is already registered.")
                return
            code = U.new_update_code()
            con.execute(
                """INSERT INTO farms (name,farmer,country,region,crop,acres,latitude,longitude,land_access,
                   water_score,soil_score,market_score,climate_score,operations_score,capital_needed,
                   expected_revenue,estimated_cost,description,verification,created_at,currency,update_key_hash)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (name, farmer, country, region, crop, acres, lat, lon, land_access, water, soil, market,
                 climate, operations, capital, revenue, costs, description, "Pending", now_iso(),
                 currency, U.hash_code(code)))
        mark_submitted("register")
        invalidate()
        st.session_state["new_farm_code"] = (name, code)
        reset_form("register")
        st.rerun()


def page_viability(farms: pd.DataFrame) -> None:
    section_header("Farm viability", "A transparent preliminary screening tool — not a credit rating, "
                                     "valuation or investment recommendation.")
    if farms.empty:
        st.info("Register a farm first.")
        return
    r = farm_picker(farms, "Select farm", "viability_farm")
    st.metric("Preliminary FarmScore", f"{int(r.farm_score)}/100")
    dimensions = pd.DataFrame({
        "Dimension": ["Water", "Soil", "Market access", "Climate resilience", "Operational capacity"],
        "Score": [r.water_score, r.soil_score, r.market_score, r.climate_score, r.operations_score]})
    left, right = st.columns([1.2, 1])
    with left:
        st.plotly_chart(px.bar(dimensions, x="Dimension", y="Score", range_y=[0, 100],
                               title="Assessment dimensions"), width="stretch")
    with right:
        st.subheader("Illustrative project economics")
        st.write("Capital required:", U.money(r.capital_needed, r.currency))
        st.write("Expected revenue:", U.money(r.expected_revenue, r.currency))
        st.write("Estimated costs:", U.money(r.estimated_cost, r.currency))
        st.write("Estimated operating surplus:", U.money(r.expected_revenue - r.estimated_cost, r.currency))
        st.caption("These figures are user-submitted estimates, not audited financial results.")
    st.subheader("Evidence and verification checklist")
    st.caption("Use this as a due-diligence guide; ticks are not saved.")
    checks = ["Identity of farmer / operating entity independently checked",
              "Land access rights and disputes reviewed by qualified local professional",
              "GPS boundary surveyed and signed off",
              "Crop and farm condition independently inspected",
              "Production history supported by records",
              "Buyer / offtake assumptions documented",
              "Budget and financial assumptions independently reviewed",
              "Environmental and social risks assessed"]
    for i, item in enumerate(checks):
        st.checkbox(item, key=f"check_{int(r.id)}_{i}")
    st.warning("Do not publish a farm as verified or solicit investment until evidence, tenure, financial "
               "assumptions and applicable legal requirements have been independently reviewed.")


def page_monitoring(farms: pd.DataFrame) -> None:
    section_header("Farm monitoring", "Record dated field updates and maintain an evidence trail.")
    show_flash()
    if farms.empty:
        st.info("No farms registered.")
        return
    r = farm_picker(farms, "Farm", "monitor_farm")
    fid = int(r["id"])
    k = lambda n: form_key("update", n)  # noqa: E731
    with st.form("update_form"):
        d = st.date_input("Update date", value=datetime.now(timezone.utc).date(),
                          max_value=datetime.now(timezone.utc).date(), key=k("date"))
        category = st.selectbox("Update category", UPDATE_CATEGORIES, key=k("cat"))
        note = st.text_area("Field observation / update", max_chars=2000, key=k("note"))
        image_url = st.text_input("Photo URL (optional, https://…)", max_chars=500, key=k("url"))
        code = ""
        if not is_admin():
            code = st.text_input("Farm update code *", type="password", max_chars=40, key=k("code"),
                                 help="Given to the farmer when the farm was registered.")
        submit = st.form_submit_button("Add farm update")
    if submit:
        note, image_url = U.clean_text(note, 2000), U.clean_text(image_url, 500)
        with db() as con:
            row = con.execute("SELECT update_key_hash FROM farms WHERE id=?", (fid,)).fetchone()
        if not note:
            st.error("Please add an observation.")
        elif image_url and not U.valid_http_url(image_url):
            st.error("The photo link must be a valid http(s) URL.")
        elif not is_admin() and not U.check_code(code, row["update_key_hash"] if row else None):
            st.error("Invalid farm update code. Only the farm operator (or an administrator) can post updates.")
        elif throttled("update", 20):
            pass
        else:
            with db() as con:
                con.execute("INSERT INTO updates (farm_id,update_date,category,note,image_url) VALUES (?,?,?,?,?)",
                            (fid, str(d), category, note, image_url))
            mark_submitted("update")
            reset_form("update")
            flash("Update saved.")
            st.rerun()
    with db() as con:
        updates = pd.read_sql_query("SELECT * FROM updates WHERE farm_id=? ORDER BY update_date DESC, id DESC",
                                    con, params=(fid,))
    st.subheader("Farm activity timeline")
    st.caption("Updates are reported by the farm operator and are not independently verified unless stated.")
    if updates.empty:
        st.info("No updates recorded yet.")
    for _, u in updates.iterrows():
        with st.container(border=True):
            st.caption(f"{e(u['update_date'])} · {e(u['category'])}")
            st.markdown(e(u["note"]))
            if u["image_url"] and U.valid_http_url(u["image_url"]):
                st.link_button("Open field photo", u["image_url"])


def page_investment(farms: pd.DataFrame) -> None:
    section_header("Investment discovery", "Register non-binding interest in a farm project. "
                                           "This prototype does not sell securities or accept funds.")
    show_flash()
    st.error("No investment is being offered through this demo. Submitting interest does not create shares, "
             "ownership, a loan or a right to returns.")
    if farms.empty:
        st.info("No farms listed.")
        return
    r = farm_picker(farms, "Farm project", "invest_farm")
    st.write(f"**{e(r['crop'])} · {e(r['region'])}, {e(r['country'])}**")
    st.write(f"Capital requirement (farmer estimate): **{U.money(r['capital_needed'], r['currency'])}**")
    k = lambda n: form_key("interest", n)  # noqa: E731
    with st.form("interest_form"):
        investor = st.text_input("Your name", max_chars=120, key=k("name"))
        email = st.text_input("Email", max_chars=254, key=k("email"))
        amount = st.number_input(f"Indicative amount of interest ({r['currency']})", min_value=0.0,
                                 max_value=1e10, value=100.0, step=100.0, key=k("amount"))
        note = st.text_area("Questions or comments", max_chars=1000, key=k("note"))
        consent = st.checkbox("I understand this is only a non-binding expression of interest and not an "
                              "investment.", key=k("consent"))
        privacy = st.checkbox("I agree that my name, email and message are stored and may be seen by the "
                              "platform administrators (see About & privacy).", key=k("privacy"))
        submit = st.form_submit_button("Register interest")
    if not submit:
        return
    investor, note = U.clean_text(investor, 120), U.clean_text(note, 1000)
    if not investor or not U.valid_email(email) or amount <= 0:
        st.error("Enter your name, a valid email and an amount greater than zero.")
    elif not (consent and privacy):
        st.error("Please tick both confirmation boxes.")
    elif throttled("interest", 30):
        pass
    else:
        with db() as con:
            con.execute("""INSERT INTO interests (farm_id,investor_name,investor_email,amount,note,created_at)
                           VALUES (?,?,?,?,?,?)""",
                        (int(r["id"]), investor, email.strip(), amount, note, now_iso()))
        mark_submitted("interest")
        reset_form("interest")
        flash("Your expression of interest has been recorded for demonstration purposes.")
        st.rerun()


def page_marketplace(farms: pd.DataFrame) -> None:
    section_header("Agricultural marketplace", "A basic directory for farm inputs, equipment and produce. "
                                               "Listings are not vetted in this prototype.")
    show_flash()
    st.warning("Verify every seller and buyer yourself. Do not pay in advance to someone you have not verified.")
    tab1, tab2 = st.tabs(["Browse listings", "Create listing"])
    with tab1:
        with db() as con:
            listings = pd.read_sql_query("SELECT * FROM listings ORDER BY created_at DESC, id DESC", con)
        if listings.empty:
            st.info("No listings yet. Add the first listing.")
        else:
            f1, f2 = st.columns([1, 2])
            typ = f1.selectbox("Listing type", ["All"] + LISTING_TYPES, key="browse_type")
            query = f2.text_input("Search title or location", max_chars=100, key="browse_query")
            if typ != "All":
                listings = listings[listings.listing_type == typ]
            if query.strip():
                q = query.strip()
                listings = listings[listings.title.str.contains(q, case=False, regex=False)
                                    | listings.location.str.contains(q, case=False, regex=False)]
            st.dataframe(listings[["listing_type", "title", "category", "location", "quantity", "price", "contact"]],
                         width="stretch", hide_index=True)
    with tab2:
        k = lambda n: form_key("listing", n)  # noqa: E731
        with st.form("listing_form"):
            typ = st.selectbox("Listing type", LISTING_TYPES, key=k("type"))
            title = st.text_input("Product or service title", max_chars=120, key=k("title"))
            category = st.selectbox("Category", LISTING_CATEGORIES, key=k("cat"))
            location = st.text_input("Location", max_chars=120, key=k("loc"))
            quantity = st.text_input("Quantity / availability", max_chars=120, key=k("qty"))
            price = st.text_input("Price / pricing basis", max_chars=120, key=k("price"))
            contact = st.text_input("Contact details", max_chars=120, key=k("contact"))
            consent = st.checkbox("I understand that these contact details will be publicly visible.",
                                  key=k("consent"))
            submit = st.form_submit_button("Publish listing")
        if submit:
            title, location, quantity, price, contact = (U.clean_text(v, 120) for v in
                                                         (title, location, quantity, price, contact))
            if not all([title, location, quantity, price, contact]):
                st.error("Complete all listing fields.")
            elif not consent:
                st.error("Please confirm that your contact details will be public.")
            elif throttled("listing", 30):
                pass
            else:
                with db() as con:
                    con.execute("""INSERT INTO listings (listing_type,title,category,location,quantity,price,
                                   contact,created_at) VALUES (?,?,?,?,?,?,?,?)""",
                                (typ, title, category, location, quantity, price, contact, now_iso()))
                mark_submitted("listing")
                reset_form("listing")
                flash("Listing published.")
                st.rerun()


def page_admin(_: pd.DataFrame) -> None:
    section_header("Administration & data exports", "Moderate records, set verification status and download data.")
    show_flash()
    if not is_admin():
        password = get_admin_password()
        if not password:
            st.error("The admin area is disabled because no ADMIN_PASSWORD is configured. Set it as an "
                     "environment variable or in Streamlit secrets.")
            return
        locked = st.session_state.get("admin_lock_until", 0) - time.time()
        if locked > 0:
            st.error(f"Too many failed attempts. Try again in {int(locked) + 1} seconds.")
            return
        with st.form("admin_login"):
            entered = st.text_input("Admin password", type="password", key="admin_pw")
            ok = st.form_submit_button("Sign in")
        if ok:
            if hmac.compare_digest(entered.encode(), password.encode()):
                st.session_state["is_admin"] = True
                st.session_state["admin_fails"] = 0
                st.rerun()
            fails = st.session_state.get("admin_fails", 0) + 1
            st.session_state["admin_fails"] = fails
            if fails >= 5:
                st.session_state["admin_lock_until"] = time.time() + 60
                st.session_state["admin_fails"] = 0
            time.sleep(1)  # slow down guessing
            st.error("Incorrect password.")
        return

    if st.button("Sign out", key="admin_signout"):
        st.session_state["is_admin"] = False
        st.rerun()

    all_farms = load_farms(include_rejected=True)
    with db() as con:
        interests = pd.read_sql_query("SELECT * FROM interests ORDER BY created_at DESC", con)
        updates = pd.read_sql_query("SELECT * FROM updates ORDER BY update_date DESC, id DESC", con)
        listings = pd.read_sql_query("SELECT * FROM listings ORDER BY created_at DESC, id DESC", con)

    t_farms, t_interest, t_updates, t_listings, t_backup = st.tabs(
        ["Farms", "Expressions of interest", "Farm updates", "Marketplace listings", "Backup"])

    def export(label: str, frame: pd.DataFrame, filename: str) -> None:
        st.dataframe(frame, width="stretch", hide_index=True)
        if not frame.empty:
            st.download_button(f"Download {label} CSV", U.csv_safe(frame).to_csv(index=False).encode("utf-8"),
                               filename, "text/csv", key=f"dl_{filename}")

    with t_farms:
        export("farms", all_farms, "agrishare_farms.csv")
        if not all_farms.empty:
            st.subheader("Set verification status / remove a farm")
            fr = farm_picker(all_farms, "Farm", "admin_farm")
            c1, c2 = st.columns([2, 1])
            status = c1.selectbox("Verification status", STATUSES,
                                  index=STATUSES.index(fr["verification"]) if fr["verification"] in STATUSES else 0,
                                  key=f"admin_status_{int(fr['id'])}")
            if c2.button("Update status", key="admin_set_status"):
                with db() as con:
                    con.execute("UPDATE farms SET verification=? WHERE id=?", (status, int(fr["id"])))
                invalidate()
                flash(f"Status of “{fr['name']}” set to {status}. ('Rejected' farms are hidden from the public.)")
                st.rerun()
            confirm = st.checkbox("Permanently delete this farm and its updates and interest records",
                                  key=f"admin_confirm_delete_{int(fr['id'])}")
            if st.button("Delete farm", disabled=not confirm, key="admin_delete_farm"):
                with db() as con:
                    for table in ("updates", "interests"):
                        con.execute(f"DELETE FROM {table} WHERE farm_id=?", (int(fr["id"]),))  # noqa: S608
                    con.execute("DELETE FROM farms WHERE id=?", (int(fr["id"]),))
                invalidate()
                flash(f"Deleted “{fr['name']}”.")
                st.rerun()
    with t_interest:
        export("expressions of interest", interests, "agrishare_interest.csv")
    with t_updates:
        export("farm updates", updates, "agrishare_updates.csv")
    with t_listings:
        export("marketplace listings", listings, "agrishare_listings.csv")
        if not listings.empty:
            st.subheader("Remove a listing")
            ids = listings["id"].tolist()
            titles = dict(zip(listings["id"], listings["title"]))
            lid = st.selectbox("Listing", ids, format_func=lambda i: f"#{i} — {titles[i]}", key="admin_listing")
            if st.button("Delete listing", key="admin_delete_listing"):
                with db() as con:
                    con.execute("DELETE FROM listings WHERE id=?", (int(lid),))
                flash(f"Listing #{lid} deleted.")
                st.rerun()
    with t_backup:
        st.caption("A full copy of the database, including personal data. Store it securely.")
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "backup.db"
            with db() as src:
                dst = sqlite3.connect(target)
                src.backup(dst)
                dst.close()
            st.download_button("Download database backup", target.read_bytes(),
                               f"agrishare_backup_{datetime.now(timezone.utc):%Y%m%d_%H%M}.db",
                               "application/octet-stream", key="dl_backup")


def page_about(_: pd.DataFrame) -> None:
    section_header("About & privacy", "How this prototype handles information.")
    st.info("This is a plain-language template, not legal advice. Have it reviewed by a qualified lawyer for "
            "each country you operate in (for example Ghana's Data Protection Act and the EU/UK GDPR) before "
            "collecting real personal data.")
    st.markdown("""
**What AgriShare Africa is.** A prototype for demonstration and validation. It does not sell securities, accept
money, verify land title or guarantee any information shown.

**What we collect.**
- *Farm registrations:* farm and farmer name, country, region, GPS point, land-access status, self-reported
  scores and financial estimates, description. **All of this is publicly visible.**
- *Field updates:* date, category, notes and an optional photo link. Publicly visible on the farm's timeline.
- *Marketplace listings:* title, location, price and contact details. **Publicly visible.**
- *Investment interest:* name, email, indicative amount and message. **Visible only to administrators.**

**Why.** To show farms to potential partners, record an evidence trail, and let interested people ask
questions. Interest forms are not offers, applications or contracts.

**Update codes.** Each farm receives a secret code when it is registered. We store only a one-way hash of it.

**Retention and deletion.** Prototype data may be deleted or reset at any time. To request access, correction
or deletion of your data, contact the administrators using the details below.

**Security.** Data is stored in a database on the hosting platform. No system is perfectly secure — do not
submit sensitive personal data such as ID numbers, bank details or precise home addresses.
""")
    st.caption(f"Contact: {CONTACT_EMAIL}" if CONTACT_EMAIL else
               "Contact: not configured — set the CONTACT_EMAIL environment variable before going live.")


# ----------------------------- Navigation ----------------------------------
PAGES = {
    "Overview": page_overview,
    "Explore farms": page_explore,
    "Register a farm": page_register,
    "Farm viability": page_viability,
    "Farm monitoring": page_monitoring,
    "Investment interest": page_investment,
    "Agricultural marketplace": page_marketplace,
    "About & privacy": page_about,
    "Admin & exports": page_admin,
}

st.sidebar.markdown("# 🌱 AgriShare Africa")
st.sidebar.caption("Farm intelligence • Market access • Investment discovery")
page = st.sidebar.radio("Navigate", list(PAGES), key="nav")
st.sidebar.divider()
st.sidebar.info("Prototype mode: investment figures and sample farms are illustrative. "
                "No money is collected and no ownership is issued.")
st.sidebar.caption("AgriShare Africa MVP • Built for validation and demonstration")

try:
    PAGES[page](load_farms())
except Exception:  # noqa: BLE001 — Streamlit's rerun/stop signals are BaseException and pass through
    log.exception("Unhandled error on page %s", page)
    st.error("Something went wrong while loading this page. Please try again; if it keeps happening, "
             "contact the site administrator.")
