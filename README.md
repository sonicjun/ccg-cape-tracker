# CCG 47-ft Cape Class SAR Fleet Tracker

A **100% free, zero-server, near-real-time maritime tracking dashboard** for the **Canadian Coast Guard (CCG) 47-ft Cape Class Motor Lifeboat** Search and Rescue (SAR) fleet.

Hosted statically on **GitHub Pages** and updated every 30 minutes via **GitHub Actions** using public AIS telemetry and Canadian government maritime data.

---

## Architecture & Highlights

- **Zero-Build Static Frontend**: Pure `index.html`, `style.css`, and `app.js` powered by CDN-hosted **MapLibre GL JS** and **Turf.js** (no Node.js, Webpack, or Vite required).
- **36 Verified 47-ft Cape Class SAR Lifeboats**: Strictly compiled from the official [CCG Fleet Database (FDAT)](https://inter-j02.dfo-mpo.gc.ca/fdat/) (`14.6 m` SAR Lifeboats), excluding unrelated vessels with "Cape" in the name (e.g., *CCGS Cape Roger*, *CCGS Cape Light*, *CCGS Cape Hurd*).
- **3-Tier Operational AIS Display**:
  - **Active / Recent (`< 2 hours`)**: Bright cyan directional arrow rotated by `COG`, displaying `SOG` and heading.
  - **Last Known (`2 hours – 7 days`)**: Amber diamond marker at last reported coordinates (`Last seen Xh/Xd ago`).
  - **Home Port Fallback (`> 7 days` or no AIS)**: Slate station beacon at the vessel's CCG SAR Home Port (`Home Port (No recent AIS)`).
- **Persistent AIS Cache Merge**: `scripts/fetch_ais.py` connects to `wss://stream.aisstream.io/v0/stream` with `permessage-deflate` compression enabled, listens for a bounded window (`90s`), and updates **only** vessels that transmitted during that run—never erasing cached positions for silent, moored, or winter-laid-up vessels.
- **Tactical Maritime Overlays**:
  - **CARTO Dark Matter** basemap
  - **OpenSeaMap** seamarks (buoys, beacons, lights)
  - **ECCC MSC GeoMet WMS** overlays (`https://geo.weather.gc.ca/geomet`) for 10m marine/surface wind and sea ice concentration
  - **200 NM SAR Range Rings** dynamically computed around each vessel's home port using **Turf.js**

---

## Repository Structure

```text
├── index.html                      # Static maritime dashboard UI
├── style.css                       # Responsive dark tactical styles (Desktop + iOS Safari)
├── app.js                          # MapLibre GL JS + Turf.js map, filters & detail drawer
├── data/
│   ├── vessels.json                # 36 CCG 47-ft Cape Class lifeboats + FDAT specs/equipment
│   └── positions.json              # Persistent AIS position cache updated by GitHub Actions
├── scripts/
│   ├── fetch_fleet.py              # Scrapes CCG FDAT specs/equipment & public Canadian MMSIs
│   ├── validate_fleet.py           # Validates schema, Canadian coordinates & duplicate MMSIs
│   └── fetch_ais.py                # AISStream WebSocket updater + persistent merge + --mock
├── .github/workflows/
│   └── update_ais.yml              # 30-minute scheduled AIS updater committing positions.json
├── start.ps1                       # Local Windows launcher (--mock support + HTTP server)
├── requirements.txt                # Python 3.11+ dependencies
├── .env.example                    # Local AISSTREAM_API_KEY template
└── .gitignore
```

---

## Quick Start (Windows PowerShell)

Run locally with simulated maritime positions across Halifax, St. Lawrence, Great Lakes, and Vancouver Island:

```powershell
.\start.ps1 --mock
```

Or run with your own live [AISStream.io](https://aisstream.io/) API key:

```powershell
Copy-Item .env.example .env
# Edit .env and set AISSTREAM_API_KEY=...
.\.venv\Scripts\python scripts\fetch_ais.py --duration 90
.\start.ps1
```

### Refreshing & Validating the Fleet Registry

```powershell
.\.venv\Scripts\python scripts\fetch_fleet.py
.\.venv\Scripts\python scripts\validate_fleet.py
```

---

## GitHub Pages & GitHub Actions Setup

1. **Create a free AISStream API Key**:
   - Sign in at [https://aisstream.io/](https://aisstream.io/) with GitHub and copy your API key.
2. **Push this repository to GitHub**:
   ```bash
   git init
   git add .
   git commit -m "Initial commit: CCG 47-ft Cape Class SAR Fleet Tracker"
   git branch -M main
   git remote add origin https://github.com/<your-username>/<your-repo>.git
   git push -u origin main
   ```
3. **Configure Repository Secret**:
   - In your GitHub repo, go to **Settings → Secrets and variables → Actions → New repository secret**.
   - Name: `AISSTREAM_API_KEY`
   - Value: *(paste your AISStream API key)*
4. **Enable GitHub Actions Write Permissions**:
   - Go to **Settings → Actions → General → Workflow permissions** and ensure **Read and write permissions** is enabled (the workflow also declares `permissions: contents: write`).
5. **Enable GitHub Pages**:
   - Go to **Settings → Pages**.
   - Under **Build and deployment**, set **Source** to **Deploy from a branch**, select branch **`main`** and folder **`/ (root)`**, then click **Save**.
6. **Trigger Initial Live AIS Run**:
   - Go to **Actions → Update AIS Vessel Positions → Run workflow** to verify live AIS updates prior to the next 30-minute cron cycle.

---

## Public Data Sources & Attribution

- **Vessel Specifications & Equipment**: Fisheries and Oceans Canada / Canadian Coast Guard Fleet Database ([CCG FDAT](https://inter-j02.dfo-mpo.gc.ca/fdat/))
- **Live AIS Telemetry**: [AISStream.io](https://aisstream.io/)
- **Nautical Seamarks**: [OpenSeaMap](https://openseamap.org/)
- **Marine Weather & Sea Ice WMS**: Environment and Climate Change Canada ([MSC GeoMet](https://eccc-msc.github.io/open-data/msc-geomet/readme_en/))
- **Basemap**: [CARTO Dark Matter](https://carto.com/basemaps) &copy; OpenStreetMap contributors
