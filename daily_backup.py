# -*- coding: utf-8 -*-
"""
BACKUP GIORNALIERO — eseguito da GitHub Actions nel repo archivio.
Per ogni titolo in watchlist.json scarica le candele intraday della giornata
(4 timeframe, solo sessione regolare), salva/fonde lo snapshot gzip, aggiorna
index.json e accoda le righe a storico.csv (l'archivio piatto scaricabile).

Il commit/push è gestito dal workflow: qui si scrivono solo i file.
Nessuna API GitHub necessaria: il repo è già in checkout locale.
"""

import gzip
import json
import os
from datetime import datetime

import pandas as pd
import yfinance as yf

TIMEFRAMES = ["5m", "15m", "30m", "1h"]
WATCHLIST = "watchlist.json"
INDEX = "archive/index.json"
STORICO = "storico.csv"


def tipo_candela(o, c):
    if c > o:
        return "verde"
    if c < o:
        return "rossa"
    return "doji"


def leggi_json(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


REGOLE_RESAMPLE = {"15m": "15min", "30m": "30min", "1h": "60min"}


def scarica_5m(symbol):
    """Dati 5m dell'intera sessione regolare, asta di chiusura inclusa.
    Una sola chiamata per titolo: i timeframe superiori si aggregano in locale."""
    df = yf.Ticker(symbol).history(
        period="1d", interval="5m", prepost=False, auto_adjust=False
    )
    if df is None or df.empty:
        return None
    df = df[["Open", "Close", "Volume"]]
    df = df[df["Volume"] > 0]
    if df.empty:
        return None
    if df.index.tz is not None:
        df.index = df.index.tz_convert("Europe/Rome")
    return df


def aggrega(df5, interval):
    """15m/30m/1h costruiti dai 5m: l'ultima candela arriva alla chiusura
    reale (17:30-17:35 per Milano), nessun troncamento alle 17:00."""
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


def main():
    watchlist = leggi_json(WATCHLIST, [])
    if not watchlist:
        print("Watchlist vuota: niente da salvare.")
        return

    indice = leggi_json(INDEX, [])
    righe_csv = []

    for voce in watchlist:
        symbol, nome = voce["s"], voce.get("n", voce["s"])
        tfs, data_str = {}, None

        df5 = scarica_5m(symbol)
        if df5 is None:
            print(f"{symbol}: nessun dato (mercato chiuso o titolo illiquido), salto.")
            continue

        for tf in TIMEFRAMES:
            df = aggrega(df5, tf)
            if df.empty:
                continue
            data_str = df.index[-1].strftime("%Y-%m-%d")
            candele = []
            for ts, o, c, v in zip(df.index, df["Open"], df["Close"], df["Volume"]):
                ora = ts.strftime("%H:%M")
                tipo = tipo_candela(o, c)
                candele.append([ora, round(float(o), 4), round(float(c), 4), int(v), tipo])
                righe_csv.append({
                    "data": data_str, "ticker": symbol, "timeframe": tf, "orario": ora,
                    "open": round(float(o), 4), "close": round(float(c), 4),
                    "volume": int(v), "tipo": tipo,
                })
            tfs[tf] = candele

        if not tfs:
            print(f"{symbol}: nessun dato (mercato chiuso o titolo illiquido), salto.")
            continue

        # snapshot: merge con eventuale salvataggio manuale dello stesso giorno
        cartella = os.path.join("archive", symbol.replace(".", "_"))
        os.makedirs(cartella, exist_ok=True)
        file_path = os.path.join(cartella, f"{data_str}.json.gz")

        payload = {"s": symbol, "n": nome, "d": data_str, "tfs": {}}
        if os.path.exists(file_path):
            with gzip.open(file_path, "rt", encoding="utf-8") as f:
                payload = json.load(f)
        payload["tfs"].update(tfs)

        with gzip.open(file_path, "wt", encoding="utf-8", compresslevel=9) as f:
            json.dump(payload, f, separators=(",", ":"))

        record = {"s": symbol, "n": nome, "d": data_str,
                  "tfs": sorted(payload["tfs"]), "p": file_path.replace(os.sep, "/")}
        indice = [r for r in indice if r["p"] != record["p"]] + [record]
        print(f"{symbol}: salvato {data_str} ({', '.join(sorted(tfs))})")

    os.makedirs("archive", exist_ok=True)
    with open(INDEX, "w", encoding="utf-8") as f:
        json.dump(indice, f, separators=(",", ":"))

    # storico.csv: archivio piatto cumulativo, senza duplicati
    if righe_csv:
        nuovo = pd.DataFrame(righe_csv)
        if os.path.exists(STORICO):
            nuovo = pd.concat([pd.read_csv(STORICO), nuovo], ignore_index=True)
        nuovo = nuovo.drop_duplicates(subset=["data", "ticker", "timeframe", "orario"], keep="last")
        nuovo = nuovo.sort_values(["data", "ticker", "timeframe", "orario"])
        nuovo.to_csv(STORICO, index=False)
        print(f"storico.csv: {len(nuovo)} righe totali.")

    print(f"Backup completato {datetime.now():%Y-%m-%d %H:%M}.")


if __name__ == "__main__":
    main()
