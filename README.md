# 📊 Volumi Intraday

Monitor minimalista delle candele di volume intraday per individuare possibili
accumuli o distribuzioni istituzionali. Nessun indicatore, nessuna analisi
tecnica: solo barre verdi/rosse con l'altezza proporzionale al volume e il
valore in K/M sopra ogni barra.

- **Verde** `#00C853` → Close > Open (pressione in acquisto)
- **Rossa** `#E53935` → Close < Open (pressione in vendita)
- **Doji** grigia → Close = Open
- Timeframe: **5m · 15m · 30m · 1h**
- Solo **sessione regolare** (pre-market e after-hours esclusi)
- Ricerca per **ticker, ISIN o nome società** (es. `RACE`, `IT0003856405`, `Ferrari`)
- Archivio storico su **GitHub** con export CSV e PNG

## Installazione locale

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Deploy su Streamlit Cloud

1. Crea un repository GitHub con `app.py` e `requirements.txt`.
2. Crea un **secondo repository privato** per l'archivio (es. `volumi-archivio`).
3. Genera un token GitHub *fine-grained* con permesso **Contents: Read and write**
   limitato al solo repo archivio.
4. Su Streamlit Cloud → App → Settings → **Secrets**:

```toml
GITHUB_TOKEN = "github_pat_xxxxxxxx"
GITHUB_REPO  = "tuo-utente/volumi-archivio"
```

Il token non compare mai nel codice.

## Come funziona l'archivio (consumo GitHub minimo)

| Scelta | Effetto |
|---|---|
| Salvataggio **solo su pulsante 💾** | zero scritture automatiche |
| Dati come array compatti `[ora, open, close, vol, tipo]` | niente chiavi JSON ripetute |
| **Compressione gzip** livello 9 | file tipici da 1–3 KB |
| Indice separato `archive/index.json` con cache in sessione | 1 solo GET per sessione |
| Snapshot letto **solo quando lo apri** | nessun download inutile |
| Stesso giorno+timeframe → **sovrascrittura** (stesso SHA) | niente file duplicati |

Un salvataggio costa **2 chiamate PUT** (snapshot + indice). Il limite API di
GitHub è 5.000 richieste/ora: anche con uso intensivo non si sfiora mai.

Se il database locale va perso non succede nulla: la fonte di verità è il repo
GitHub, ricaricato all'apertura della scheda Archivio.

## Limiti noti

- yfinance fornisce dati intraday solo per gli ultimi ~60 giorni: lo storico
  di lungo periodo si costruisce salvando gli snapshot nel tempo.
- I volumi Yahoo possono avere ~1 minuto di ritardo sulle borse europee.
- Il PNG si esporta dall'icona fotocamera in alto a destra sul grafico.

## Backup automatico giornaliero (watchlist)

I titoli aggiunti alla watchlist con il pulsante **⭐** vengono salvati
automaticamente ogni sera feriale da un workflow GitHub Actions nel repo
archivio — anche quando l'app Streamlit è addormentata.

File da caricare nel **repo archivio** (`volumi-archivio`):

```
daily_backup.py
.github/workflows/backup_giornaliero.yml
```

⚠️ Il file YML va dentro la cartella `.github/workflows/` (creala su GitHub
scrivendo il percorso completo nel nome file durante l'upload).

- Orario: **21:45 UTC** nei giorni feriali (22:45/23:45 ora italiana),
  sempre dopo la chiusura sia di Milano sia di Wall Street, tutto l'anno.
- Nessun token da configurare: il workflow usa il token automatico di Actions.
- Ogni sera salva i 4 timeframe (5m·15m·30m·1h) di ogni titolo in watchlist.
- Aggiorna anche `storico.csv`, l'archivio piatto cumulativo.
- Test manuale: tab **Actions** del repo → "Backup giornaliero volumi" → *Run workflow*.

## Download dell'archivio

Dalla scheda **Archivio** dell'app:
- **📦 ZIP completo** — l'intero repository archivio (tutti gli snapshot).
- **📄 storico.csv** — tutte le candele in un unico CSV apribile in Excel.
