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
    try:
        fi = yf.Ticker(symbol).fast_info
        return fi.get("last_price"), fi.get("previous_close")
    except Exception:
        return None, None


# ----------------------------------------------------------------- GRAFICO --


def fmt_px(p: float) -> str:
    """Prezzo con decimali adeguati (titoli sotto 1€/$ mostrano 4 decimali)."""
    return f"{p:.4f}" if p < 1 else (f"{p:.3f}" if p < 10 else f"{p:.2f}")


ORO = "#FFD54F"          # bordo delle barre con volume anomalo
SOGLIA_ANOMALIA = 3.0    # volume ≥ 3× la mediana di giornata = possibile mano forte


def costruisci_figura(df: pd.DataFrame, titolo: str, timeframe: str,
                      zoom_pct: int = 100) -> go.Figure:
    """Grafico a barre di volume: altezza = volume, colore = direzione candela.
    Etichetta su due righe: volume (K/M) + prezzo chiusura + var % vs apertura
    del giorno. Barre ≥ SOGLIA_ANOMALIA × mediana evidenziate in oro (×N).

    Mobile UX:
    - dragmode disattivato → il dito che scorre sul grafico NON zooma:
      lo scroll verticale della pagina ha sempre la priorità;
    - navigatore orizzontale spesso sotto il grafico (rangeslider) per
      scorrere/zoomare nel tempo trascinando le maniglie;
    - zoom_pct (10-100) limita l'asse Y per ingrandire le barre piccole."""
    n = len(df)
    # etichette grandi, ridotte solo quando le barre diventano tante
    size_txt = 17 if n <= 14 else 15 if n <= 26 else 13 if n <= 44 else 11

    apertura_giorno = float(df["Open"].iloc[0])
    mediana = float(df["Volume"].median()) or 1.0
    rvol = df["Volume"] / mediana
    anomala = rvol >= SOGLIA_ANOMALIA

    labels = [ts.strftime("%H:%M") for ts in df.index]
    x_idx = list(range(n))                       # asse lineare: serve al rangeslider
    passo = max(1, n // 10)
    tickvals = x_idx[::passo]
    ticktext = labels[::passo]

    colori = [colore_candela(o, c) for o, c in zip(df["Open"], df["Close"])]
    bordi = [ORO if a else "rgba(0,0,0,0)" for a in anomala]
    spess = [2.5 if a else 0 for a in anomala]

    var_giorno = (df["Close"] / apertura_giorno - 1) * 100
    etichette = [
        (f"<b>{fmt_vol(v)} ×{r:.1f}</b><br>{fmt_px(c)} {p:+.2f}%"
         if a else
         f"<b>{fmt_vol(v)}</b><br>{fmt_px(c)} {p:+.2f}%")
        for v, c, p, r, a in zip(df["Volume"], df["Close"], var_giorno, rvol, anomala)
    ]
    var_candela = (df["Close"] / df["Open"] - 1) * 100

    fig = go.Figure(
        go.Bar(
            x=x_idx,
            y=df["Volume"],
            marker_color=colori,
            marker_line=dict(color=bordi, width=spess),
            text=etichette,
            textposition="outside",
            textfont=dict(family="Inter", size=size_txt, color=TESTO),
            textangle=0,
            cliponaxis=True,   # con lo zoom verticale le etichette non escono dal grafico
            customdata=list(zip(
                df["Open"].round(4), df["Close"].round(4),
                var_candela.round(2), var_giorno.round(2),
                [fmt_vol(v) for v in df["Volume"]], rvol.round(1),
                labels,
            )),
            hovertemplate=(
                "<b>%{customdata[6]}</b><br>"
                "Open %{customdata[0]}  ·  Close %{customdata[1]}<br>"
                "Var candela %{customdata[2]}%<br>"
                "Var da apertura giorno <b>%{customdata[3]}%</b><br>"
                "Volume <b>%{customdata[4]}</b> · ×%{customdata[5]} vs mediana"
                "<extra></extra>"
            ),
        )
    )
    fig.update_layout(
        title=dict(text=titolo, font=dict(family="Inter", size=14, color=TESTO_SOFT)),
        paper_bgcolor=SFONDO,
        plot_bgcolor=SFONDO,
        font=dict(family="Inter", color=TESTO),
        margin=dict(l=10, r=10, t=48, b=10),
        height=680,
        bargap=0.25,
        showlegend=False,
        hoverlabel=dict(bgcolor=PANNELLO, font_family="Inter"),
        dragmode=False,        # il drag col dito NON zooma: la pagina scorre
    )
    fig.update_xaxes(
        showgrid=False,
        tickvals=tickvals, ticktext=ticktext,
        tickfont=dict(size=12, color=TESTO_SOFT),
        range=[-0.6, n - 0.4],
        # "scrollbar" orizzontale: spessa, trascinabile, sotto il grafico
        rangeslider=dict(
            visible=True,
            thickness=0.16,
            bgcolor=PANNELLO,
            bordercolor=GRIGLIA,
            borderwidth=1,
        ),
    )
    fig.update_yaxes(
        gridcolor=GRIGLIA, gridwidth=0.4, zeroline=False,
        tickfont=dict(size=12, color=TESTO_SOFT),
        fixedrange=True,       # niente zoom accidentale in verticale
        # zoom Y esplicito via slider: 100% = tutto, 10% = ingrandisce le barre piccole
        range=[0, float(df["Volume"].max()) * 1.35 * max(10, min(100, zoom_pct)) / 100],
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


def salva_snapshot(symbol, nome, tf_label, df: pd.DataFrame):
    """Salva il grafico corrente. Un solo file per titolo+giorno contenente
    tutti i timeframe salvati: il salvataggio manuale si fonde (merge) con
    quello automatico notturno senza duplicati. Costo: 2 PUT."""
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
        aggiorna = st.button("↻ Aggiorna", use_container_width=True)
    with c4:
        st.write("")
        salva = st.button("💾 Salva", use_container_width=True)
    with c5:
        st.write("")
        watch = st.button("⭐ Watchlist", use_container_width=True,
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
                fig = costruisci_figura(
                    df, f"Volumi {tf_label} — {df.index[-1]:%d/%m/%Y}", tf_label, zoom_v
                )
                st.plotly_chart(fig, use_container_width=True, config={
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
                    elif salva_snapshot(symbol, nome, tf_label, df):
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
                    st.plotly_chart(
                        costruisci_figura(df_s, f"Volumi {tf_lbl} — {snap['d']}", tf_lbl, zoom_a),
                        use_container_width=True,
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
