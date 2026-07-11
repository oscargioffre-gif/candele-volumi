# -*- coding: utf-8 -*-
"""
VOLUMI INTRADAY — Monitor candele di volume
============================================
Visualizza SOLO i volumi di acquisto/vendita intraday (nessun indicatore,
nessuna analisi tecnica) per individuare a colpo d'occhio possibili
accumuli/distribuzioni istituzionali.

- Candela VERDE : Close > Open  (#00C853)
- Candela ROSSA : Close < Open  (#E53935)
- Doji GRIGIA   : Close = Open
- Altezza barra = volume; valore in K/M sopra ogni barra
- Timeframe: 5m / 15m / 30m / 1h — solo sessione regolare (no pre/after market)
- Archivio storico con persistenza su GitHub (JSON gzip, consumo minimo)

Deploy: Streamlit Cloud + repo GitHub. Token in st.secrets, mai nel codice.
"""

import base64
import gzip
import io
import json
from datetime import datetime, timezone

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
import yfinance as yf

# ---------------------------------------------------------------- COSTANTI --

VERDE = "#00C853"
ROSSO = "#E53935"
GRIGIO = "#8A8F98"
SFONDO = "#101010"
PANNELLO = "#181818"
GRIGLIA = "#2A2A2A"
TESTO = "#EAEAEA"
TESTO_SOFT = "#9AA0A6"

TIMEFRAMES = {"5 min": "5m", "15 min": "15m", "30 min": "30m", "1 ora": "1h"}

INDEX_PATH = "archive/index.json"   # indice leggero (poche centinaia di byte a record)

# ------------------------------------------------------------------- SETUP --

st.set_page_config(
    page_title="Volumi Intraday",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown(
    f"""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;800&display=swap');

    html, body, [class*="css"], .stApp {{
        font-family: 'Inter', sans-serif;
        background-color: {SFONDO};
        color: {TESTO};
    }}
    .block-container {{ padding-top: 1.2rem; padding-bottom: 2rem; }}

    /* Header dashboard */
    .vh-header {{
        display: flex; flex-wrap: wrap; align-items: baseline; gap: 14px;
        border-bottom: 1px solid {GRIGLIA}; padding-bottom: 10px; margin-bottom: 4px;
    }}
    .vh-ticker  {{ font-size: 1.9rem; font-weight: 800; letter-spacing: .5px; }}
    .vh-name    {{ font-size: 1.0rem; color: {TESTO_SOFT}; }}
    .vh-price   {{ font-size: 1.6rem; font-weight: 600; margin-left: auto; }}
    .vh-up      {{ color: {VERDE}; }}
    .vh-down    {{ color: {ROSSO}; }}
    .vh-meta    {{ font-size: .8rem; color: {TESTO_SOFT}; width: 100%; }}

    .stTextInput input {{
        background: {PANNELLO}; color: {TESTO}; border: 1px solid {GRIGLIA};
        border-radius: 8px; font-family: 'Inter', sans-serif;
    }}
    .stButton button {{
        background: {PANNELLO}; color: {TESTO}; border: 1px solid {GRIGLIA};
        border-radius: 8px; font-weight: 500; transition: border-color .15s ease;
    }}
    .stButton button:hover {{ border-color: {VERDE}; color: {VERDE}; }}

    div[data-baseweb="segmented-control"] {{ background: {PANNELLO}; }}

    /* Slider "dita grandi": maniglia e traccia ben spesse, facili da afferrare */
    div[data-baseweb="slider"] div[role="slider"] {{
        width: 30px !important; height: 30px !important;
        background: {VERDE} !important; border: 3px solid #0B3D20 !important;
    }}
    div[data-baseweb="slider"] > div > div {{ height: 10px !important; }}
    </style>
    """,
    unsafe_allow_html=True,
)

# ------------------------------------------------------------- FORMAT UTIL --


def fmt_vol(v: float) -> str:
    """724883 -> '724.9K', 1234567 -> '1.23M', 950 -> '950'."""
    v = float(v)
    if v >= 1_000_000:
        return f"{v / 1_000_000:.2f}M"
    if v >= 1_000:
        return f"{v / 1_000:.1f}K"
    return f"{v:.0f}"


def colore_candela(o: float, c: float) -> str:
    if c > o:
        return VERDE
    if c < o:
        return ROSSO
    return GRIGIO


def tipo_candela(o: float, c: float) -> str:
    if c > o:
        return "verde"
    if c < o:
        return "rossa"
    return "doji"


# ------------------------------------------------------- RICERCA TICKER/ISIN --


@st.cache_data(ttl=24 * 3600, show_spinner=False)
def cerca_simbolo(query: str):
    """Risolve ticker, ISIN o nome società tramite la ricerca Yahoo Finance.
    Ritorna (symbol, nome, exchange) oppure None."""
    try:
        r = requests.get(
            "https://query2.finance.yahoo.com/v1/finance/search",
            params={"q": query.strip(), "quotesCount": 6, "newsCount": 0},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        quotes = [q for q in r.json().get("quotes", []) if q.get("symbol")]
        if not quotes:
            return None
        # Preferisci azioni; a parità, il primo risultato
        azioni = [q for q in quotes if q.get("quoteType") == "EQUITY"]
        q = (azioni or quotes)[0]
        nome = q.get("shortname") or q.get("longname") or q["symbol"]
        return q["symbol"], nome, q.get("exchDisp", "")
    except Exception:
        return None


# ------------------------------------------------------------- DATI INTRADAY --


# Aggregazione locale: si scarica SEMPRE il 5m e si costruiscono 15m/30m/1h
# in pandas. Vantaggi: l'ultima candela arriva alla chiusura reale del mercato
# (17:30-17:35 per Milano, asta di chiusura inclusa) e i quattro timeframe
# sono sempre coerenti tra loro, senza i troncamenti delle candele native Yahoo.
REGOLE_RESAMPLE = {"15m": "15min", "30m": "30min", "1h": "60min"}


def _pulisci(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    df = df[["Open", "Close", "Volume"]].copy()
    df = df[df["Volume"] > 0]
    if not df.empty and df.index.tz is not None:
        df.index = df.index.tz_convert("Europe/Rome")
    return df


def _aggrega_da_5m(df5: pd.DataFrame, interval: str) -> pd.DataFrame:
    """15m/30m/1h costruiti dai 5m: la candela finale ingloba anche gli
    ultimi scambi e l'asta di chiusura, qualunque sia il timeframe."""
    if interval == "5m":
        return df5
    agg = (
        df5.resample(REGOLE_RESAMPLE[interval], origin="start_day",
                     label="left", closed="left")
           .agg(Open=("Open", "first"), Close=("Close", "last"),
                Volume=("Volume", "sum"))
           .dropna()
    )
    return agg[agg["Volume"] > 0]


@st.cache_data(ttl=60, show_spinner=False)
def scarica_intraday(symbol: str, interval: str) -> pd.DataFrame:
    """Candele intraday della giornata, SOLO sessione regolare, fino alla
    chiusura reale del mercato (nessun taglio alle 17:00):
    1) finestra esplicita da mezzanotte a ora+1h sui dati 5m;
    2) se vuota (weekend/festivo), ultima sessione disponibile sempre a 5m;
    3) fallback estremo: candele native del timeframe richiesto."""
    tk = yf.Ticker(symbol)
    ora = pd.Timestamp.now(tz="Europe/Rome")

    df5 = pd.DataFrame()
    if ora.weekday() < 5:  # nel weekend niente sessione odierna: dritti al fallback
        df5 = _pulisci(tk.history(
            start=ora.normalize(), end=ora + pd.Timedelta(hours=1),
            interval="5m", prepost=False, auto_adjust=False,
        ))
    if df5.empty:  # mercato chiuso oggi: ultima sessione disponibile
        df5 = _pulisci(tk.history(
            period="1d", interval="5m", prepost=False, auto_adjust=False,
        ))
    if not df5.empty:
        return _aggrega_da_5m(df5, interval)

    # fallback estremo: dati nativi al timeframe richiesto
    return _pulisci(tk.history(
        period="1d", interval=interval, prepost=False, auto_adjust=False,
    ))


@st.cache_data(ttl=60, show_spinner=False)
def prezzo_attuale(symbol: str):
    """Ultimo prezzo + chiusura del giorno precedente. La chiusura precedente
    è il pilastro della % cumulativa: se fast_info non la fornisce o è sporca,
    si ricava dallo storico giornaliero ufficiale (candele daily)."""
    ultimo, prec = None, None
    try:
        fi = yf.Ticker(symbol).fast_info
        ultimo = fi.get("last_price")
        prec = fi.get("previous_close")
    except Exception:
        pass
    try:
        h = yf.Ticker(symbol).history(period="7d", interval="1d",
                                      prepost=False, auto_adjust=False)
        if h is not None and len(h) >= 2:
            prec_daily = float(h["Close"].iloc[-2])
            # se fast_info manca o diverge oltre lo 0.5% dal daily ufficiale,
            # vince il daily (fast_info a volte è ritardato o rettificato)
            if not prec or abs(prec / prec_daily - 1) > 0.005:
                prec = prec_daily
            if not ultimo:
                ultimo = float(h["Close"].iloc[-1])
    except Exception:
        pass
    return ultimo, prec


# ----------------------------------------------------------------- GRAFICO --


def fmt_px(p: float) -> str:
    """Prezzo con decimali adeguati (titoli sotto 1€/$ mostrano 4 decimali)."""
    return f"{p:.4f}" if p < 1 else (f"{p:.3f}" if p < 10 else f"{p:.2f}")


ORO = "#FFD54F"          # bordo delle barre con volume anomalo
SOGLIA_ANOMALIA = 3.0    # volume ≥ 3× la mediana di giornata = possibile mano forte
BARRE_VISIBILI = 4       # candele per pagina: colonne larghissime, etichette senza sovrapposizioni
VERDE_TESTO = "#00E676"  # variazione positiva vs chiusura precedente
ROSSO_TESTO = "#FF5252"  # variazione negativa vs chiusura precedente


def costruisci_figura(df: pd.DataFrame, titolo: str, timeframe: str,
                      zoom_pct: int = 100, chiusura_prec: float = None,
                      inizio: int = None, fine: int = None) -> go.Figure:
    """Grafico a barre di volume: altezza = volume, colore barra = direzione
    candela. Etichetta su tre righe sopra ogni barra:
      1) volume (K/M), con ×N se anomalo;
      2) prezzo di chiusura della candela;
      3) VARIAZIONE % — grande (20px) e prioritaria — della chiusura di
         OGNI candela rispetto alla CHIUSURA DELLA CANDELA PRECEDENTE
         (la prima candela si confronta con la chiusura del giorno prima).
    Colore della %: VERDE se >= 0, ROSSO se < 0, tassativamente.
    Barre ≥ SOGLIA_ANOMALIA × mediana bordate in oro con il moltiplicatore ×N.

    'chiusura_prec' (chiusura del giorno precedente) fa da base alla PRIMA
    candela della giornata; per i vecchi snapshot senza questo dato si usa
    l'apertura della prima candela.

    Mobile UX: di default sono visibili le ultime BARRE_VISIBILI candele,
    larghe e ben distanziate; le precedenti si raggiungono trascinando il
    navigatore sotto il grafico. Drag disattivato: la pagina scorre sempre."""
    n = len(df)

    # ------------------------------------------------ MATEMATICA ETICHETTE --
    # (1) VARIAZIONE CUMULATIVA vs CHIUSURA DI IERI (dato principale, grande):
    #     var_cum[i] = (Close[i] / chiusura_prec - 1) * 100
    #     L'ultima candela coincide col dato ufficiale di giornata del broker.
    #     Fallback per i vecchi snapshot senza chiusura_prec: apertura del giorno.
    if chiusura_prec and chiusura_prec > 0:
        base_cum, base_nome = float(chiusura_prec), "chiusura ieri"
    else:
        base_cum, base_nome = float(df["Open"].iloc[0]), "apertura giorno"
    var_cum = (df["Close"] / base_cum - 1) * 100
    # colore rigorosamente legato al segno CUMULATIVO: VERDE >= 0, ROSSO < 0
    colori_cum = [VERDE if p >= 0 else ROSSO for p in var_cum]

    # (2) DELTA INTRA-CANDELA (dato secondario, piccolo):
    #     var_delta[i] = (Close[i] / Close[i-1] - 1) * 100
    #     Prima candela del grafico: rispetto al proprio Open (nessun i-1).
    rif_delta = df["Close"].shift(1)
    rif_delta.iloc[0] = float(df["Open"].iloc[0])
    var_delta = (df["Close"] / rif_delta - 1) * 100

    mediana = float(df["Volume"].median()) or 1.0
    rvol = df["Volume"] / mediana
    anomala = rvol >= SOGLIA_ANOMALIA

    labels = [ts.strftime("%H:%M") for ts in df.index]
    x_idx = list(range(n))                       # asse lineare (finestra paginata)
    passo = max(1, n // 10)
    tickvals = x_idx[::passo]
    ticktext = labels[::passo]

    colori = [colore_candela(o, c) for o, c in zip(df["Open"], df["Close"])]
    bordi = [ORO if a else "rgba(0,0,0,0)" for a in anomala]
    spess = [2.5 if a else 0 for a in anomala]

    # limite superiore asse Y: serve a decidere dove sta il testo di ogni barra
    y_max = float(df["Volume"].max()) * 1.65 * max(10, min(100, zoom_pct)) / 100

    # ------------------------------------------------- STRUTTURE ETICHETTE --
    # Regola ADATTIVA (risolve in modo definitivo le sovrapposizioni):
    # - barra ALTA  (>=16% del grafico): prezzo + Δ centrati DENTRO la barra;
    # - barra BASSA (<16%): non c'è spazio fisico → prezzo + Δ salgono nel
    #   blocco SOPRA la barra, in coda alla %. Un blocco unico impilato da
    #   Plotly non può sovrapporsi per costruzione.
    # Tra volume e % c'è sempre una riga distanziatrice (9px) per dare aria.
    SOGLIA_INTERNO = 0.16
    corta = [v < y_max * SOGLIA_INTERNO for v in df["Volume"]]
    SPAZIO = "<br><span style='font-size:9px'>\u200b</span><br>"

    etichette, testo_interno = [], []
    for v, c, pc, pdlt, r, a, col, bassa in zip(
        df["Volume"], df["Close"], var_cum, var_delta, rvol, anomala, colori_cum, corta
    ):
        blocco = (
            f"<b>{fmt_vol(v)}{f' ×{r:.1f}' if a else ''}</b>"
            f"{SPAZIO}"
            f"<span style='font-size:26px;color:{col}'><b>{pc:+.2f}%</b></span>"
        )
        interno = (
            f"<b>{fmt_px(c)}</b><br>"
            f"<span style='font-size:15px'>Δ {pdlt:+.2f}%</span>"
        )
        if bassa:  # barra troppo bassa: prezzo e Δ salgono sopra, dentro niente
            blocco += (
                f"<br><b>{fmt_px(c)}</b>"
                f"<br><span style='font-size:15px;color:{TESTO_SOFT}'>Δ {pdlt:+.2f}%</span>"
            )
            interno = ""
        etichette.append(blocco)
        testo_interno.append(interno)

    var_candela = (df["Close"] / df["Open"] - 1) * 100

    # centro verticale della parte VISIBILE delle barre alte: mai oltre il
    # taglio dello zoom (per le basse il testo interno è vuoto)
    y_centro = [min(v / 2, y_max * 0.45) for v in df["Volume"]]

    fig = go.Figure(
        go.Bar(
            x=x_idx,
            y=df["Volume"],
            marker_color=colori,
            marker_line=dict(color=bordi, width=spess),
            text=etichette,
            textposition="outside",
            # font base (volume e prezzo): la % ha il proprio span da 20px
            textfont=dict(family="Inter", size=16, color=TESTO),
            textangle=0,
            cliponaxis=True,   # con lo zoom verticale le etichette non escono dal grafico
            customdata=list(zip(
                df["Open"].round(4), df["Close"].round(4),
                var_candela.round(2), var_cum.round(2),
                [fmt_vol(v) for v in df["Volume"]], rvol.round(1),
                labels, var_delta.round(2),
            )),
            hovertemplate=(
                "<b>%{customdata[6]}</b><br>"
                "Open %{customdata[0]}  ·  Close %{customdata[1]}<br>"
                f"Var vs {base_nome} ({fmt_px(base_cum)}) " + "<b>%{customdata[3]}%</b><br>"
                "Δ vs candela prec. %{customdata[7]}%  ·  Var candela %{customdata[2]}%<br>"
                "Volume <b>%{customdata[4]}</b> · ×%{customdata[5]} vs mediana"
                "<extra></extra>"
            ),
        )
    )
    # testo DENTRO le barre: prezzo + Δ, bianco pieno per il massimo contrasto
    fig.add_trace(
        go.Scatter(
            x=x_idx, y=y_centro,
            mode="text",
            text=testo_interno,
            textfont=dict(family="Inter", size=18, color="#FFFFFF"),
            hoverinfo="skip",
            showlegend=False,
            cliponaxis=True,
        )
    )
    fig.update_layout(
        title=dict(text=titolo, font=dict(family="Inter", size=14, color=TESTO_SOFT)),
        paper_bgcolor=SFONDO,
        plot_bgcolor=SFONDO,
        font=dict(family="Inter", color=TESTO),
        margin=dict(l=10, r=10, t=48, b=10),
        height=500,   # basso e largo: proporzioni da smartphone
        bargap=0.10,           # colonne larghissime: quasi tutto lo spazio è barra
        showlegend=False,
        hoverlabel=dict(bgcolor=PANNELLO, font=dict(family="Inter", size=14)),
        dragmode=False,        # il drag col dito NON zooma: la pagina scorre
    )
    # finestra visibile: indici [inizio, fine) passati dalla paginazione a
    # frecce (session_state); default = le ultime BARRE_VISIBILI candele
    if fine is None or fine > n:
        fine = n
    if inizio is None or inizio < 0:
        inizio = max(0, fine - BARRE_VISIBILI)
    fig.update_xaxes(
        showgrid=False,
        tickvals=tickvals, ticktext=ticktext,
        tickfont=dict(size=13, color=TESTO),
        range=[inizio - 0.6, fine - 0.4],
        rangeslider=dict(visible=False),   # niente barra Plotly: frecce native
    )
    fig.update_yaxes(
        gridcolor=GRIGLIA, gridwidth=0.4, zeroline=False,
        tickfont=dict(size=13, color=TESTO),
        fixedrange=True,       # niente zoom accidentale in verticale
        # zoom Y esplicito via slider: 100% = tutto, 10% = ingrandisce le barre piccole
        range=[0, y_max],
    )
    return fig


# Configurazione Plotly condivisa: via i pulsanti di zoom/pan (fonte di tocchi
# accidentali su mobile); restano fotocamera PNG e doppio tap per il reset.
CONFIG_GRAFICO = {
    "displaylogo": False,
    "scrollZoom": False,
    "doubleClick": "reset",
    "modeBarButtonsToRemove": [
        "zoom2d", "pan2d", "select2d", "lasso2d",
        "zoomIn2d", "zoomOut2d", "autoScale2d",
    ],
}


# ------------------------------------------------ PAGINAZIONE CON LE FRECCE --
# Sostituisce il rangeslider Plotly: due pulsanti nativi Streamlit, grandi e
# comodi su mobile. L'offset (candele indietro dalla fine) vive in
# st.session_state: 0 = finestra sulle ULTIME candele (default).


def azzera_pagina_se_cambiato(chiave: str, contesto: str):
    """Riporta la finestra alle ultime candele quando cambia titolo/timeframe."""
    if st.session_state.get(f"{chiave}_ctx") != contesto:
        st.session_state[f"{chiave}_ctx"] = contesto
        st.session_state[chiave] = 0


def naviga_finestra(n: int, labels: list, chiave: str):
    """Renderizza '◀ Precedenti · info finestra · Successive ▶' e ritorna gli
    indici [inizio, fine) della finestra corrente. Gestisce i limiti: le frecce
    si disattivano da sole a inizio e fine serie (nessun IndexError possibile)."""
    if chiave not in st.session_state:
        st.session_state[chiave] = 0
    max_off = max(0, n - BARRE_VISIBILI)

    c_sx, c_info, c_dx = st.columns([1, 3, 1])
    prec = c_sx.button("◀ Precedenti", key=f"{chiave}_prec", width="stretch",
                       disabled=st.session_state[chiave] >= max_off)
    succ = c_dx.button("Successive ▶", key=f"{chiave}_succ", width="stretch",
                       disabled=st.session_state[chiave] <= 0)
    if prec:
        st.session_state[chiave] = min(st.session_state[chiave] + BARRE_VISIBILI, max_off)
    if succ:
        st.session_state[chiave] = max(st.session_state[chiave] - BARRE_VISIBILI, 0)

    off = min(st.session_state[chiave], max_off)   # clamp anche se n è diminuito
    fine = n - off
    inizio = max(0, fine - BARRE_VISIBILI)
    tot_pagine = max(1, -(-n // BARRE_VISIBILI))
    pagina = max(1, tot_pagine - (-(-off // BARRE_VISIBILI)))
    c_info.markdown(
        f"<p style='text-align:center;color:{TESTO_SOFT};margin:6px 0 0'>"
        f"🕐 {labels[inizio]} – {labels[fine - 1]} &nbsp;·&nbsp; "
        f"pagina {pagina} di {tot_pagine}</p>",
        unsafe_allow_html=True,
    )
    return inizio, fine


# --------------------------------------------------- PERSISTENZA SU GITHUB --
# Strategia a consumo minimo:
#   * salvataggio SOLO su richiesta (pulsante), mai in automatico
#   * dati come array compatti [ora, open, close, volume] senza chiavi ripetute
#   * compressione gzip prima dell'upload (~80-90% in meno)
#   * indice separato piccolissimo, cache in sessione per evitare GET ripetuti
#   * lettura di un grafico archiviato solo quando l'utente lo apre


def gh_config():
    token = st.secrets.get("GITHUB_TOKEN", "")
    repo = st.secrets.get("GITHUB_REPO", "")   # es. "utente/volumi-archivio"
    return token, repo


def gh_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "volumi-intraday",
    }


def gh_get(path):
    """Ritorna (bytes, sha) del file, oppure (None, None) se non esiste."""
    token, repo = gh_config()
    r = requests.get(
        f"https://api.github.com/repos/{repo}/contents/{path}",
        headers=gh_headers(token), timeout=15,
    )
    if r.status_code != 200:
        return None, None
    j = r.json()
    return base64.b64decode(j["content"]), j["sha"]


def gh_put(path, raw: bytes, messaggio: str, sha=None):
    token, repo = gh_config()
    body = {"message": messaggio, "content": base64.b64encode(raw).decode()}
    if sha:
        body["sha"] = sha
    r = requests.put(
        f"https://api.github.com/repos/{repo}/contents/{path}",
        headers=gh_headers(token), json=body, timeout=20,
    )
    return r.status_code in (200, 201)


def carica_indice(forza=False):
    """Indice archivio, con cache di sessione per non consumare API."""
    if not forza and "gh_index" in st.session_state:
        return st.session_state["gh_index"]
    raw, sha = gh_get(INDEX_PATH)
    indice = json.loads(raw.decode()) if raw else []
    st.session_state["gh_index"] = indice
    st.session_state["gh_index_sha"] = sha
    return indice


def salva_snapshot(symbol, nome, tf_label, df: pd.DataFrame, chiusura_prec=None):
    """Salva il grafico corrente. Un solo file per titolo+giorno contenente
    tutti i timeframe salvati: il salvataggio manuale si fonde (merge) con
    quello automatico notturno senza duplicati. Costo: 2 PUT.
    'pc' = chiusura del giorno precedente: serve alle % anche in archivio."""
    data_str = df.index[-1].strftime("%Y-%m-%d")
    tf = TIMEFRAMES[tf_label]
    file_path = f"archive/{symbol.replace('.', '_')}/{data_str}.json.gz"

    candele = [
        [ts.strftime("%H:%M"), round(float(o), 4), round(float(c), 4),
         int(v), tipo_candela(o, c)]
        for ts, o, c, v in zip(df.index, df["Open"], df["Close"], df["Volume"])
    ]

    raw_old, sha_esistente = gh_get(file_path)
    if raw_old:  # merge con snapshot già esistente per lo stesso giorno
        payload = json.loads(gzip.decompress(raw_old).decode())
    else:
        payload = {"s": symbol, "n": nome, "d": data_str, "tfs": {}}
    if chiusura_prec and not payload.get("pc"):
        payload["pc"] = round(float(chiusura_prec), 4)
    payload["tfs"][tf] = candele

    raw = gzip.compress(json.dumps(payload, separators=(",", ":")).encode(), 9)
    ok = gh_put(file_path, raw, f"snapshot {symbol} {data_str} {tf}", sha_esistente)
    if not ok:
        return False

    indice = carica_indice()
    record = {"s": symbol, "n": nome, "d": data_str,
              "tfs": sorted(payload["tfs"]), "p": file_path}
    indice = [r for r in indice if r["p"] != file_path] + [record]
    ok = gh_put(
        INDEX_PATH,
        json.dumps(indice, separators=(",", ":")).encode(),
        f"index: {symbol} {data_str} {tf}",
        st.session_state.get("gh_index_sha"),
    )
    if ok:  # aggiorna cache locale senza altri GET
        st.session_state["gh_index"] = indice
        raw_idx, sha_idx = gh_get(INDEX_PATH)
        st.session_state["gh_index_sha"] = sha_idx
    return ok


def carica_snapshot(path):
    raw, _ = gh_get(path)
    if not raw:
        return None
    return json.loads(gzip.decompress(raw).decode())


# ------------------------------------------------------------------ WATCHLIST --
# I titoli in watchlist vengono salvati AUTOMATICAMENTE ogni sera dal workflow
# GitHub Actions (daily_backup.py) nel repo archivio, anche ad app spenta.

WATCHLIST_PATH = "watchlist.json"


def carica_watchlist():
    raw, sha = gh_get(WATCHLIST_PATH)
    return (json.loads(raw.decode()) if raw else []), sha


def toggle_watchlist(symbol, nome):
    """Aggiunge o rimuove il titolo dalla watchlist. Ritorna 'aggiunto'/'rimosso'/None."""
    wl, sha = carica_watchlist()
    simboli = [w["s"] for w in wl]
    if symbol in simboli:
        wl = [w for w in wl if w["s"] != symbol]
        esito = "rimosso"
    else:
        wl.append({"s": symbol, "n": nome})
        esito = "aggiunto"
    ok = gh_put(WATCHLIST_PATH,
                json.dumps(wl, separators=(",", ":")).encode(),
                f"watchlist: {esito} {symbol}", sha)
    return esito if ok else None


# ---------------------------------------------------------------- INTERFACCIA --

tab_live, tab_archivio = st.tabs(["📊  Live", "🗄️  Archivio"])

# ================================================================== TAB LIVE ==
with tab_live:
    c1, c2, c3, c4, c5 = st.columns([3, 2, 1, 1, 1])
    with c1:
        query = st.text_input(
            "Ticker · ISIN · Nome società",
            placeholder="es. RACE · IT0003856405 · Ferrari",
            key="query",
        )
    with c2:
        tf_label = st.radio("Timeframe", list(TIMEFRAMES), horizontal=True, index=0)
    with c3:
        st.write("")
        aggiorna = st.button("↻ Aggiorna", width="stretch")
    with c4:
        st.write("")
        salva = st.button("💾 Salva", width="stretch")
    with c5:
        st.write("")
        watch = st.button("⭐ Watchlist", width="stretch",
                          help="Aggiungi/rimuovi dal salvataggio automatico serale")

    if aggiorna:
        scarica_intraday.clear()
        prezzo_attuale.clear()

    if query:
        risultato = cerca_simbolo(query)
        if not risultato:
            st.warning("Titolo non trovato. Prova con il ticker esatto o l'ISIN.")
        else:
            symbol, nome, borsa = risultato
            df = scarica_intraday(symbol, TIMEFRAMES[tf_label])

            if df.empty:
                st.info("Nessun dato intraday disponibile ora (mercato chiuso o titolo illiquido).")
            else:
                ultimo, chiusura_prec = prezzo_attuale(symbol)
                classe = ""
                var_txt = ""
                if ultimo and chiusura_prec:
                    var = (ultimo / chiusura_prec - 1) * 100
                    classe = "vh-up" if var >= 0 else "vh-down"
                    var_txt = f" ({var:+.2f}%)"
                prezzo_txt = f"{ultimo:,.3f}{var_txt}" if ultimo else "—"
                agg = datetime.now().strftime("%H:%M:%S")

                st.markdown(
                    f"""
                    <div class="vh-header">
                      <span class="vh-ticker">{symbol}</span>
                      <span class="vh-name">{nome} · {borsa}</span>
                      <span class="vh-price {classe}">{prezzo_txt}</span>
                      <span class="vh-meta">Sessione regolare · {tf_label} · aggiornato {agg}</span>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

                zoom_v = st.slider(
                    "🔍 Zoom verticale — riduci per ingrandire le barre piccole",
                    min_value=10, max_value=100, value=100, step=10, format="%d%%",
                    key="zoom_live",
                )

                # Paginazione: i click sulle frecce vengono processati PRIMA di
                # costruire la figura (il grafico appare comunque sopra le
                # frecce grazie al container segnaposto) → refresh immediato.
                azzera_pagina_se_cambiato("pag_live", f"{symbol}_{tf_label}")
                box_grafico = st.container()
                etichette_orari = [ts.strftime("%H:%M") for ts in df.index]
                inizio, fine = naviga_finestra(len(df), etichette_orari, "pag_live")

                fig = costruisci_figura(
                    df, f"Volumi {tf_label} — {df.index[-1]:%d/%m/%Y}", tf_label,
                    zoom_v, chiusura_prec, inizio, fine,
                )
                with box_grafico:
                    st.plotly_chart(fig, width="stretch", config={
                        **CONFIG_GRAFICO,
                        "toImageButtonOptions": {"format": "png", "filename": f"{symbol}_volumi", "scale": 2},
                    })

                # Export CSV (il PNG si scarica dall'icona fotocamera del grafico)
                csv = df.assign(Tipo=[tipo_candela(o, c) for o, c in zip(df["Open"], df["Close"])])
                st.download_button(
                    "⬇ Esporta CSV",
                    csv.to_csv(index_label="Orario").encode(),
                    file_name=f"{symbol}_{TIMEFRAMES[tf_label]}.csv",
                    mime="text/csv",
                )

                if salva:
                    token, repo = gh_config()
                    if not token or not repo:
                        st.error("Configura GITHUB_TOKEN e GITHUB_REPO nei Secrets di Streamlit.")
                    elif salva_snapshot(symbol, nome, tf_label, df, chiusura_prec):
                        st.success(f"Salvato in archivio: {symbol} · {df.index[-1]:%d/%m/%Y} · {tf_label}")
                    else:
                        st.error("Salvataggio non riuscito. Verifica token e nome repository.")

                if watch:
                    token, repo = gh_config()
                    if not token or not repo:
                        st.error("Configura GITHUB_TOKEN e GITHUB_REPO nei Secrets di Streamlit.")
                    else:
                        esito = toggle_watchlist(symbol, nome)
                        if esito == "aggiunto":
                            st.success(f"⭐ {symbol} in watchlist: salvataggio automatico ogni sera (4 timeframe).")
                        elif esito == "rimosso":
                            st.info(f"{symbol} rimosso dalla watchlist.")
                        else:
                            st.error("Operazione non riuscita. Verifica token e repository.")
    else:
        st.markdown(
            f"<p style='color:{TESTO_SOFT}'>Inserisci un ticker, un ISIN o il nome di una "
            "società e premi INVIO. Barre verdi = chiusura sopra l'apertura, rosse = sotto. "
            "L'altezza è il volume scambiato: le barre anomale possono segnalare accumulo "
            "o distribuzione istituzionale.</p>",
            unsafe_allow_html=True,
        )

# ============================================================== TAB ARCHIVIO ==
with tab_archivio:
    token, repo = gh_config()
    if not token or not repo:
        st.info("Archivio non configurato: aggiungi GITHUB_TOKEN e GITHUB_REPO nei Secrets.")
    else:
        col_a, col_b = st.columns([1, 5])
        with col_a:
            if st.button("↻ Ricarica indice"):
                carica_indice(forza=True)
        indice = carica_indice()

        # --- watchlist: titoli salvati automaticamente ogni sera -------------
        wl, _ = carica_watchlist()
        if wl:
            st.markdown(
                f"<p style='color:{TESTO_SOFT}'>⭐ Salvataggio automatico serale attivo per: "
                f"<b style='color:{TESTO}'>{', '.join(w['s'] for w in wl)}</b> "
                "(gestisci con il pulsante ⭐ nella scheda Live)</p>",
                unsafe_allow_html=True,
            )

        if not indice:
            st.markdown(
                f"<p style='color:{TESTO_SOFT}'>Archivio vuoto. Salva un grafico dalla "
                "scheda Live (💾) o aggiungi titoli alla watchlist (⭐): il backup "
                "notturno inizierà a popolare lo storico da solo.</p>",
                unsafe_allow_html=True,
            )
        else:
            df_idx = pd.DataFrame(indice).rename(
                columns={"s": "Ticker", "n": "Nome", "d": "Data", "p": "path"}
            )
            f1, f2 = st.columns(2)
            with f1:
                filtro_t = st.selectbox("Ticker", ["Tutti"] + sorted(df_idx["Ticker"].unique()))
            with f2:
                filtro_d = st.selectbox("Data", ["Tutte"] + sorted(df_idx["Data"].unique(), reverse=True))

            vista = df_idx.copy()
            if filtro_t != "Tutti":
                vista = vista[vista["Ticker"] == filtro_t]
            if filtro_d != "Tutte":
                vista = vista[vista["Data"] == filtro_d]
            vista = vista.sort_values(["Data", "Ticker"], ascending=[False, True])

            scelte = [f"{r.Ticker} · {r.Data}" for r in vista.itertuples()]
            if scelte:
                sel = st.selectbox("Grafico archiviato", scelte)
                path_sel = vista.iloc[scelte.index(sel)]["path"]
                snap = carica_snapshot(path_sel)
                if snap:
                    tf_disponibili = [k for k, v in TIMEFRAMES.items() if v in snap["tfs"]]
                    tf_lbl = st.radio("Timeframe salvati", tf_disponibili, horizontal=True)
                    candele = snap["tfs"][TIMEFRAMES[tf_lbl]]
                    df_s = pd.DataFrame(candele, columns=["Orario", "Open", "Close", "Volume", "Tipo"])
                    df_s.index = pd.to_datetime(snap["d"] + " " + df_s["Orario"])
                    st.markdown(
                        f"""
                        <div class="vh-header">
                          <span class="vh-ticker">{snap['s']}</span>
                          <span class="vh-name">{snap['n']}</span>
                          <span class="vh-meta">Archivio · {snap['d']} · {tf_lbl}</span>
                        </div>
                        """,
                        unsafe_allow_html=True,
                    )
                    zoom_a = st.slider(
                        "🔍 Zoom verticale — riduci per ingrandire le barre piccole",
                        min_value=10, max_value=100, value=100, step=10, format="%d%%",
                        key="zoom_arch",
                    )
                    azzera_pagina_se_cambiato("pag_arch", f"{path_sel}_{tf_lbl}")
                    box_arch = st.container()
                    orari_arch = [ts.strftime("%H:%M") for ts in df_s.index]
                    inizio_a, fine_a = naviga_finestra(len(df_s), orari_arch, "pag_arch")
                    with box_arch:
                        st.plotly_chart(
                            costruisci_figura(df_s, f"Volumi {tf_lbl} — {snap['d']}", tf_lbl,
                                              zoom_a, snap.get("pc"), inizio_a, fine_a),
                            width="stretch",
                            config=CONFIG_GRAFICO,
                        )
                else:
                    st.error("Impossibile leggere lo snapshot dal repository.")
            else:
                st.markdown(
                    f"<p style='color:{TESTO_SOFT}'>Nessun grafico corrisponde ai filtri.</p>",
                    unsafe_allow_html=True,
                )

            # --- download archivio -------------------------------------------
            st.divider()
            d1, d2 = st.columns(2)
            with d1:
                if st.button("📦 Prepara archivio ZIP completo"):
                    with st.spinner("Preparazione ZIP dal repository…"):
                        r = requests.get(
                            f"https://api.github.com/repos/{repo}/zipball",
                            headers=gh_headers(token), timeout=120,
                        )
                    if r.status_code == 200:
                        st.download_button(
                            "⬇ Scarica archivio_volumi.zip", r.content,
                            file_name="archivio_volumi.zip", mime="application/zip",
                        )
                    else:
                        st.error("Download non riuscito. Verifica token e repository.")
            with d2:
                if st.button("📄 Prepara storico CSV completo"):
                    raw_csv, _ = gh_get("storico.csv")
                    if raw_csv:
                        st.download_button(
                            "⬇ Scarica storico.csv", raw_csv,
                            file_name="storico.csv", mime="text/csv",
                        )
                    else:
                        st.info("storico.csv non ancora creato: appare dopo il primo backup notturno.")
