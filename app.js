/**
 * CCG 47-ft Cape Class SAR Fleet Maritime Tracker
 * Pure static JS frontend powered by MapLibre GL JS + Turf.js
 */

(function () {
  "use strict";

  const TWO_HOURS_MS = 2 * 60 * 60 * 1000;
  const SEVEN_DAYS_MS = 7 * 24 * 60 * 60 * 1000;
  const REFRESH_INTERVAL_MS = 5 * 60 * 1000;

  const GEOMET_BASE = "https://geo.weather.gc.ca/geomet";
  const GEOMET_WIND_CANDIDATES = [
    "GDPS_15km_Winds_10m",
    "GDPS_15km_WindSpd_TGL_10",
    "GDPS.ETA_UU",
    "HRDPS.CONTINENTAL_UU",
  ];
  const GEOMET_ICE_CANDIDATES = [
    "GDPS_15km_IceConcentration_SFC",
    "OCEAN.GIOPS.2D_IFR",
    "OCEAN.GIOPS.2D_GL",
    "GDPS.ETA_GL",
  ];

  const REGION_BOUNDS = {
    All: [
      [-134.5, 41.5],
      [-52.0, 56.5],
    ],
    Atlantic: [
      [-66.5, 44.0],
      [-55.5, 52.0],
    ],
    Central: [
      [-90.5, 41.7],
      [-60.5, 51.0],
    ],
    Western: [
      [-133.5, 48.0],
      [-122.5, 55.0],
    ],
  };

  const state = {
    vessels: [],
    positionsByMmsi: {},
    updatedAtUtc: null,
    feedSource: "aisstream",
    enriched: [],
    filtered: [],
    regionFilter: "All",
    statusFilter: "all",
    searchQuery: "",
    selectedVesselId: null,
    map: null,
    mapLoaded: false,
    baseLayerIds: [],
    hoverPopup: null,
    resolvedWms: {
      wind: null,
      ice: null,
    },
  };

  /* =========================================================
     Formatting & Utility Helpers
     ========================================================= */

  function escapeHtml(str) {
    return String(str ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function formatRelativeAge(ageMs) {
    if (ageMs == null || !Number.isFinite(ageMs)) return "Never";
    const clamped = Math.max(0, ageMs);
    const mins = Math.floor(clamped / 60000);
    if (mins < 1) return "Just now";
    if (mins < 60) return `${mins}m ago`;
    const hours = Math.floor(mins / 60);
    if (hours < 24) {
      const remMins = mins % 60;
      return remMins > 0 && hours < 6 ? `${hours}h ${remMins}m ago` : `${hours}h ago`;
    }
    const days = Math.floor(hours / 24);
    const remHours = hours % 24;
    return days < 7 && remHours > 0 ? `${days}d ${remHours}h ago` : `${days}d ago`;
  }

  function formatUtcTimestamp(isoStr) {
    if (!isoStr) return "No AIS report";
    const d = new Date(isoStr);
    if (Number.isNaN(d.getTime())) return isoStr;
    return d.toISOString().replace("T", " ").replace(/\.\d+Z$/, " UTC").replace("Z", " UTC");
  }

  function toDmsCoord(deg, isLat) {
    const abs = Math.abs(Number(deg));
    const d = Math.floor(abs);
    const m = ((abs - d) * 60).toFixed(2).padStart(5, "0");
    const hemi = isLat ? (deg >= 0 ? "N" : "S") : deg >= 0 ? "E" : "W";
    const padDeg = String(d).padStart(isLat ? 2 : 3, "0");
    return `${padDeg}°${m}'${hemi}`;
  }

  function formatDmsPair(lat, lon) {
    if (lat == null || lon == null) return "--";
    return `${toDmsCoord(lat, true)}  ${toDmsCoord(lon, false)}`;
  }

  /* =========================================================
     Data Loading & 3-Tier Status Computation
     ========================================================= */

  async function loadData(isBackgroundRefresh = false) {
    const cacheBust = `t=${Math.floor(Date.now() / 30000)}`;
    const vesselsPromise = isBackgroundRefresh
      ? Promise.resolve(state.vessels)
      : fetch(`data/vessels.json?${cacheBust}`).then((r) => {
          if (!r.ok) throw new Error(`Failed to load vessels.json (${r.status})`);
          return r.json();
        });

    const positionsPromise = fetch(`data/positions.json?${cacheBust}`)
      .then((r) => (r.ok ? r.json() : { updated_at_utc: null, source: "none", positions: {} }))
      .catch(() => ({ updated_at_utc: null, source: "none", positions: {} }));

    const [vessels, positionsPayload] = await Promise.all([vesselsPromise, positionsPromise]);

    state.vessels = Array.isArray(vessels) ? vessels : [];
    state.positionsByMmsi = positionsPayload?.positions || {};
    state.updatedAtUtc = positionsPayload?.updated_at_utc || null;
    state.feedSource = positionsPayload?.source || "aisstream";

    recomputeFleetState();
  }

  function classifyVessel(vessel, nowMs) {
    const pos = vessel.mmsi ? state.positionsByMmsi[vessel.mmsi] : null;
    let tier = "fallback";
    let tierLabel = "Home Port (No recent AIS)";
    let ageMs = null;

    if (pos && pos.timestamp_utc) {
      const tsMs = Date.parse(pos.timestamp_utc);
      if (!Number.isNaN(tsMs)) {
        ageMs = Math.max(0, nowMs - tsMs);
        if (ageMs < TWO_HOURS_MS) {
          tier = "active";
          tierLabel = "Active / Recent";
        } else if (ageMs <= SEVEN_DAYS_MS) {
          tier = "last_known";
          tierLabel = `Last seen ${formatRelativeAge(ageMs)}`;
        }
      }
    }

    const useAisCoords = tier === "active" || tier === "last_known";
    const displayLat = useAisCoords ? Number(pos.lat) : Number(vessel.home_lat);
    const displayLon = useAisCoords ? Number(pos.lon) : Number(vessel.home_lon);
    const sog = useAisCoords && pos.sog != null ? Number(pos.sog) : null;
    const cog = useAisCoords && pos.cog != null ? Number(pos.cog) : 0;
    const heading = useAisCoords && pos.heading != null ? Number(pos.heading) : null;

    return {
      ...vessel,
      pos: pos || null,
      tier,
      tierLabel,
      ageMs,
      displayLat,
      displayLon,
      sog,
      cog,
      heading,
    };
  }

  function recomputeFleetState() {
    const nowMs = Date.now();
    const tierOrder = { active: 0, last_known: 1, fallback: 2 };

    state.enriched = state.vessels
      .map((v) => classifyVessel(v, nowMs))
      .sort((a, b) => {
        if (tierOrder[a.tier] !== tierOrder[b.tier]) {
          return tierOrder[a.tier] - tierOrder[b.tier];
        }
        return a.name.localeCompare(b.name);
      });

    updateSummaryBar();
    applyFilters();

    if (state.selectedVesselId) {
      const sel = state.enriched.find((v) => v.id === state.selectedVesselId);
      if (sel) populateDetailDrawer(sel);
    }
  }

  function applyFilters() {
    const q = state.searchQuery.trim().toLowerCase();

    state.filtered = state.enriched.filter((v) => {
      if (state.regionFilter !== "All" && v.region !== state.regionFilter) {
        return false;
      }
      if (state.statusFilter !== "all" && v.tier !== state.statusFilter) {
        return false;
      }
      if (q) {
        const hay = `${v.name} ${v.home_port} ${v.mmsi || ""} ${v.region}`.toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });

    renderRoster();
    syncMapSources();
  }

  /* =========================================================
     Summary Bar, Roster & Slide-Over Detail Drawer UI
     ========================================================= */

  function updateSummaryBar() {
    const counts = { active: 0, last_known: 0, fallback: 0 };
    for (const v of state.enriched) {
      counts[v.tier] = (counts[v.tier] || 0) + 1;
    }

    document.getElementById("kpi-total").textContent = String(state.enriched.length);
    document.getElementById("kpi-active").textContent = String(counts.active);
    document.getElementById("kpi-last-known").textContent = String(counts.last_known);
    document.getElementById("kpi-fallback").textContent = String(counts.fallback);

    const updatedEl = document.getElementById("last-updated");
    if (state.updatedAtUtc) {
      const age = Date.now() - Date.parse(state.updatedAtUtc);
      updatedEl.textContent = `${state.updatedAtUtc.slice(11, 16)} UTC (${formatRelativeAge(age)})`;
      updatedEl.title = formatUtcTimestamp(state.updatedAtUtc);
    } else {
      updatedEl.textContent = "Home Port Baseline";
    }

    const badge = document.getElementById("feed-mode-badge");
    badge.hidden = false;
    if (state.feedSource === "mock") {
      badge.textContent = "MOCK SIM";
      badge.className = "feed-badge is-mock";
    } else {
      badge.textContent = "LIVE AIS";
      badge.className = "feed-badge is-live";
    }
  }

  function renderRoster() {
    const listEl = document.getElementById("vessel-list");
    const countEl = document.getElementById("roster-count");

    const total = state.enriched.length;
    const shown = state.filtered.length;
    countEl.textContent = shown === total ? `${total} Vessels` : `${shown} of ${total} Vessels`;

    if (shown === 0) {
      listEl.innerHTML = `<div class="empty-roster">No Cape Class lifeboats match the current filter criteria.</div>`;
      return;
    }

    listEl.innerHTML = state.filtered
      .map((v) => {
        const isSel = v.id === state.selectedVesselId ? "selected" : "";
        const dotClass =
          v.tier === "active"
            ? "dot-active"
            : v.tier === "last_known"
            ? "dot-last-known"
            : "dot-fallback";

        let rightTop = "";
        let rightSub = "";
        if (v.tier === "active") {
          const spd = v.sog != null ? `${v.sog.toFixed(1)} kt` : "0.0 kt";
          rightTop = `<div class="vessel-metric metric-active">${spd} &bull; ${Math.round(v.cog)}&deg;</div>`;
          rightSub = `<div class="vessel-age">${escapeHtml(formatRelativeAge(v.ageMs))}</div>`;
        } else if (v.tier === "last_known") {
          rightTop = `<div class="vessel-metric metric-known">Last Known</div>`;
          rightSub = `<div class="vessel-age">${escapeHtml(v.tierLabel)}</div>`;
        } else {
          rightTop = `<div class="vessel-metric metric-fallback">Home Port</div>`;
          rightSub = `<div class="vessel-age">No recent AIS</div>`;
        }

        return `
          <button type="button" class="vessel-item ${isSel}" data-vessel-id="${escapeHtml(v.id)}" role="listitem">
            <div class="vessel-item-left">
              <span class="status-dot ${dotClass}"></span>
              <div style="min-width:0">
                <div class="vessel-item-title">${escapeHtml(v.name)}</div>
                <div class="vessel-item-sub">${escapeHtml(v.home_port)} &bull; ${escapeHtml(v.region)}</div>
              </div>
            </div>
            <div class="vessel-item-right">
              ${rightTop}
              ${rightSub}
            </div>
          </button>
        `;
      })
      .join("");
  }

  function populateEquipList(ulId, items) {
    const ul = document.getElementById(ulId);
    if (!Array.isArray(items) || items.length === 0) {
      ul.innerHTML = `<li>Standard Cape Class SAR fitment</li>`;
      return;
    }
    ul.innerHTML = items.map((item) => `<li>${escapeHtml(item)}</li>`).join("");
  }

  function populateDetailDrawer(v) {
    const statusBadge = document.getElementById("drawer-status-badge");
    statusBadge.className = `tier-badge ${v.tier}`;
    statusBadge.textContent =
      v.tier === "active"
        ? "Active / Recent"
        : v.tier === "last_known"
        ? "Last Known"
        : "Home Port Fallback";

    document.getElementById("drawer-region-badge").textContent = `${v.region} Region`;
    document.getElementById("drawer-vessel-name").textContent = v.name;
    document.getElementById("drawer-mmsi-line").textContent = v.mmsi
      ? `MMSI: ${v.mmsi}  •  ID: ${v.id}`
      : `MMSI: Unverified  •  ID: ${v.id}`;

    // Telemetry cells
    const isTracked = v.tier === "active" || v.tier === "last_known";
    document.getElementById("drawer-sog").textContent =
      isTracked && v.sog != null ? `${v.sog.toFixed(1)} kt` : "—";

    if (isTracked) {
      const cogTxt = `${Math.round(v.cog)}°`;
      const hdgTxt = v.heading != null ? `${v.heading}° HDG` : "COG";
      document.getElementById("drawer-cog").textContent = `${cogTxt} (${hdgTxt})`;
      document.getElementById("drawer-nav-status").textContent =
        v.pos?.nav_status || "Reported via AIS";
    } else {
      document.getElementById("drawer-cog").textContent = "—";
      document.getElementById("drawer-nav-status").textContent = "Home Port (No recent AIS)";
    }

    document.getElementById("drawer-coords").textContent = `${v.displayLat.toFixed(4)}°, ${v.displayLon.toFixed(4)}°`;
    document.getElementById("drawer-coords-dms").textContent = formatDmsPair(v.displayLat, v.displayLon);

    if (v.pos && v.pos.timestamp_utc) {
      document.getElementById("drawer-ais-time").textContent = formatUtcTimestamp(v.pos.timestamp_utc);
      const relNote =
        v.tier === "fallback"
          ? `Stale (${formatRelativeAge(v.ageMs)}) — displaying Home Port`
          : formatRelativeAge(v.ageMs);
      document.getElementById("drawer-ais-rel").textContent = relNote;
    } else {
      document.getElementById("drawer-ais-time").textContent = "No recent AIS";
      document.getElementById("drawer-ais-rel").textContent = "Positioned at CCG SAR Home Port";
    }

    // Station & Specs
    document.getElementById("drawer-home-port").textContent = v.home_port;
    document.getElementById("drawer-home-coords").textContent = `${v.home_lat.toFixed(4)}°, ${v.home_lon.toFixed(4)}°`;
    document.getElementById("drawer-year-built").textContent = String(v.year_built);

    document.getElementById("drawer-spec-len").textContent = `${v.specs?.length_m ?? 14.6} m`;
    document.getElementById("drawer-spec-spd").textContent = `${v.specs?.max_speed_kt ?? 25} kt`;
    document.getElementById("drawer-spec-rng").textContent = `${v.specs?.range_nm ?? 200} NM`;
    document.getElementById("drawer-spec-crew").textContent = String(v.specs?.crew ?? 4);

    // Equipment
    populateEquipList("drawer-eq-propulsion", v.equipment?.propulsion);
    populateEquipList("drawer-eq-navigation", v.equipment?.navigation);
    populateEquipList("drawer-eq-comms", v.equipment?.communications);

    const vfLink = document.getElementById("drawer-vf-link");
    if (vfLink) {
      if (v.mmsi) {
        vfLink.href = `https://www.vesselfinder.com/vessels/details/${encodeURIComponent(v.mmsi)}`;
        vfLink.hidden = false;
      } else {
        vfLink.removeAttribute("href");
        vfLink.hidden = true;
      }
    }

    const fdatLink = document.getElementById("drawer-fdat-link");
    fdatLink.href = v.source_url;
  }

  function selectVessel(vesselId, shouldFly = true) {
    const vessel = state.enriched.find((v) => v.id === vesselId);
    if (!vessel) return;

    state.selectedVesselId = vesselId;
    populateDetailDrawer(vessel);

    const drawer = document.getElementById("detail-drawer");
    drawer.classList.add("open");
    drawer.setAttribute("aria-hidden", "false");

    // Close mobile left menu if open so map + detail sheet are unobstructed
    document.getElementById("sidebar").classList.remove("mobile-open");
    document.getElementById("mobile-menu-btn").setAttribute("aria-expanded", "false");

    renderRoster();
    syncMapSources();

    if (shouldFly && state.map) {
      const currentZoom = state.map.getZoom();
      state.map.flyTo({
        center: [vessel.displayLon, vessel.displayLat],
        zoom: Math.max(currentZoom, 7.8),
        speed: 1.35,
        essential: true,
      });
    }
  }

  function closeDetailDrawer() {
    state.selectedVesselId = null;
    const drawer = document.getElementById("detail-drawer");
    drawer.classList.remove("open");
    drawer.setAttribute("aria-hidden", "true");
    renderRoster();
    syncMapSources();
  }

  /* =========================================================
     MapLibre GL JS Custom Tactical Icons & GeoJSON Builders
     ========================================================= */

  function createCanvasImageData(size, drawFn) {
    const canvas = document.createElement("canvas");
    canvas.width = size;
    canvas.height = size;
    const ctx = canvas.getContext("2d");
    drawFn(ctx, size);
    const imgData = ctx.getImageData(0, 0, size, size);
    return { width: size, height: size, data: imgData.data };
  }

  function registerTacticalIcons(map) {
    // 1. Active / Recent (< 2h): Bright Cyan Directional Arrow (points North at 0° COG)
    const activeIcon = createCanvasImageData(64, (ctx, s) => {
      const c = s / 2;
      ctx.save();
      // Subtle cyan glow halo
      const grad = ctx.createRadialGradient(c, c, 4, c, c, 28);
      grad.addColorStop(0, "rgba(34, 211, 238, 0.42)");
      grad.addColorStop(1, "rgba(34, 211, 238, 0)");
      ctx.fillStyle = grad;
      ctx.beginPath();
      ctx.arc(c, c, 28, 0, Math.PI * 2);
      ctx.fill();

      // Sleek directional SAR chevron/arrow pointing UP (North = 0°)
      ctx.beginPath();
      ctx.moveTo(c, 9);
      ctx.lineTo(c + 15, 50);
      ctx.lineTo(c, 41);
      ctx.lineTo(c - 15, 50);
      ctx.closePath();
      ctx.fillStyle = "#22d3ee";
      ctx.fill();
      ctx.lineWidth = 2.5;
      ctx.strokeStyle = "#04131d";
      ctx.stroke();

      // Crisp white bow highlight
      ctx.beginPath();
      ctx.arc(c, 28, 3, 0, Math.PI * 2);
      ctx.fillStyle = "#ecfeff";
      ctx.fill();
      ctx.restore();
    });

    // 2. Last Known (2h - 7d): Amber Tactical Diamond Marker
    const lastKnownIcon = createCanvasImageData(52, (ctx, s) => {
      const c = s / 2;
      ctx.save();
      const grad = ctx.createRadialGradient(c, c, 3, c, c, 22);
      grad.addColorStop(0, "rgba(245, 158, 11, 0.35)");
      grad.addColorStop(1, "rgba(245, 158, 11, 0)");
      ctx.fillStyle = grad;
      ctx.beginPath();
      ctx.arc(c, c, 22, 0, Math.PI * 2);
      ctx.fill();

      ctx.translate(c, c);
      ctx.rotate(Math.PI / 4);
      ctx.fillStyle = "#f59e0b";
      ctx.strokeStyle = "#080f1c";
      ctx.lineWidth = 2.5;
      ctx.beginPath();
      ctx.rect(-9.5, -9.5, 19, 19);
      ctx.fill();
      ctx.stroke();

      ctx.fillStyle = "#fef3c7";
      ctx.fillRect(-3, -3, 6, 6);
      ctx.restore();
    });

    // 3. Home Port Fallback (> 7d or no AIS): Muted Slate Station Beacon
    const fallbackIcon = createCanvasImageData(44, (ctx, s) => {
      const c = s / 2;
      ctx.save();
      ctx.beginPath();
      ctx.arc(c, c, 10.5, 0, Math.PI * 2);
      ctx.fillStyle = "rgba(30, 41, 59, 0.92)";
      ctx.fill();
      ctx.lineWidth = 2.5;
      ctx.strokeStyle = "#94a3b8";
      ctx.stroke();

      ctx.beginPath();
      ctx.arc(c, c, 4, 0, Math.PI * 2);
      ctx.fillStyle = "#cbd5e1";
      ctx.fill();
      ctx.restore();
    });

    map.addImage("icon-active-arrow", activeIcon, { pixelRatio: 2 });
    map.addImage("icon-last-known", lastKnownIcon, { pixelRatio: 2 });
    map.addImage("icon-home-port", fallbackIcon, { pixelRatio: 2 });
  }

  function buildVesselsGeoJSON() {
    return {
      type: "FeatureCollection",
      features: state.filtered.map((v) => ({
        type: "Feature",
        geometry: {
          type: "Point",
          coordinates: [v.displayLon, v.displayLat],
        },
        properties: {
          id: v.id,
          name: v.name,
          mmsi: v.mmsi || "Unverified",
          region: v.region,
          home_port: v.home_port,
          tier: v.tier,
          tierLabel: v.tierLabel,
          sog: v.sog != null ? v.sog.toFixed(1) : "",
          cog: v.cog || 0,
          ageText: formatRelativeAge(v.ageMs),
          selected: v.id === state.selectedVesselId ? 1 : 0,
        },
      })),
    };
  }

  function buildRangeRingsGeoJSON() {
    if (!window.turf) {
      return { type: "FeatureCollection", features: [] };
    }
    const features = state.filtered.map((v) => {
      const rangeNm = v.specs?.range_nm || 200;
      const circle = window.turf.circle([v.home_lon, v.home_lat], rangeNm, {
        steps: 72,
        units: "nauticalmiles",
        properties: {
          id: v.id,
          name: v.name,
          region: v.region,
          selected: v.id === state.selectedVesselId ? 1 : 0,
        },
      });
      return circle;
    });
    return { type: "FeatureCollection", features };
  }

  function buildSelectedTetherGeoJSON() {
    if (!state.selectedVesselId || !window.turf) {
      return { type: "FeatureCollection", features: [] };
    }
    const v = state.enriched.find((x) => x.id === state.selectedVesselId);
    if (!v || v.tier === "fallback") {
      return { type: "FeatureCollection", features: [] };
    }
    const distNm = window.turf.distance(
      [v.home_lon, v.home_lat],
      [v.displayLon, v.displayLat],
      { units: "nauticalmiles" }
    );
    if (distNm < 0.5) {
      return { type: "FeatureCollection", features: [] };
    }
    const line = window.turf.greatCircle(
      [v.home_lon, v.home_lat],
      [v.displayLon, v.displayLat],
      { npoints: 32 }
    );
    return { type: "FeatureCollection", features: [line] };
  }

  function syncMapSources() {
    if (!state.map || !state.mapLoaded) return;

    const vesselsSrc = state.map.getSource("vessels-source");
    if (vesselsSrc) vesselsSrc.setData(buildVesselsGeoJSON());

    const ringsSrc = state.map.getSource("sar-rings-source");
    if (ringsSrc) ringsSrc.setData(buildRangeRingsGeoJSON());

    const tetherSrc = state.map.getSource("selected-tether-source");
    if (tetherSrc) tetherSrc.setData(buildSelectedTetherGeoJSON());
  }

  /* =========================================================
     ECCC GeoMet WMS Layer Probing & Mounting
     ========================================================= */

  function buildGeometTileUrl(layerName) {
    return (
      `${GEOMET_BASE}?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetMap` +
      `&BBOX={bbox-epsg-3857}&CRS=EPSG:3857&WIDTH=256&HEIGHT=256` +
      `&LAYERS=${encodeURIComponent(layerName)}&FORMAT=image/png&TRANSPARENT=TRUE`
    );
  }

  async function resolveGeometLayer(kind, candidates, statusElId) {
    if (state.resolvedWms[kind]) return state.resolvedWms[kind];

    const statusEl = document.getElementById(statusElId);
    const originalText = statusEl.textContent;
    statusEl.textContent = "Connecting to ECCC GeoMet WMS...";
    statusEl.classList.remove("is-warn");

    for (const layerName of candidates) {
      const testUrl =
        `${GEOMET_BASE}?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetMap` +
        `&BBOX=-15000000,5000000,-5000000,10000000&CRS=EPSG:3857&WIDTH=64&HEIGHT=64` +
        `&LAYERS=${encodeURIComponent(layerName)}&FORMAT=image/png&TRANSPARENT=TRUE`;

      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), 5500);
      try {
        const resp = await fetch(testUrl, { signal: ctrl.signal });
        clearTimeout(timer);
        const ct = (resp.headers.get("content-type") || "").toLowerCase();
        if (resp.ok && ct.includes("image/png")) {
          state.resolvedWms[kind] = layerName;
          statusEl.textContent = `Active: ${layerName}`;
          return layerName;
        }
      } catch (_err) {
        clearTimeout(timer);
      }
    }

    statusEl.textContent = "GeoMet WMS unreachable on network";
    statusEl.classList.add("is-warn");
    setTimeout(() => {
      if (!state.resolvedWms[kind]) {
        statusEl.textContent = originalText;
        statusEl.classList.remove("is-warn");
      }
    }, 6000);
    return null;
  }

  async function toggleGeometOverlay(kind, enabled, candidates, statusElId, checkboxEl) {
    if (!state.map || !state.mapLoaded) return;
    const sourceId = `geomet-${kind}-source`;
    const layerId = `geomet-${kind}-layer`;

    if (!enabled) {
      if (state.map.getLayer(layerId)) {
        state.map.setLayoutProperty(layerId, "visibility", "none");
      }
      return;
    }

    if (state.map.getLayer(layerId)) {
      state.map.setLayoutProperty(layerId, "visibility", "visible");
      return;
    }

    const layerName = await resolveGeometLayer(kind, candidates, statusElId);
    if (!layerName) {
      checkboxEl.checked = false;
      return;
    }

    if (!state.map.getSource(sourceId)) {
      state.map.addSource(sourceId, {
        type: "raster",
        tiles: [buildGeometTileUrl(layerName)],
        tileSize: 256,
        attribution: "&copy; ECCC / MSC GeoMet",
      });
    }

    // Insert weather raster below SAR range rings & vessel symbols
    const beforeId = state.map.getLayer("seamarks-layer") ? "seamarks-layer" : undefined;
    state.map.addLayer(
      {
        id: layerId,
        type: "raster",
        source: sourceId,
        paint: {
          "raster-opacity": kind === "ice" ? 0.62 : 0.55,
          "raster-fade-duration": 200,
        },
      },
      beforeId
    );
  }

  /* =========================================================
     Map Initialization & Interactive Handlers
     ========================================================= */

  function initMap() {
    const map = new window.maplibregl.Map({
      container: "map",
      style: "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json",
      center: [-94.5, 51.2],
      zoom: 3.35,
      minZoom: 2,
      maxZoom: 17,
      attributionControl: true,
    });

    state.map = map;

    map.addControl(new window.maplibregl.NavigationControl({ visualizePitch: true }), "top-right");
    map.addControl(new window.maplibregl.ScaleControl({ maxWidth: 140, unit: "nautical" }), "bottom-left");

    // Gracefully suppress transient third-party tile fetch timeouts in console
    map.on("error", (evt) => {
      if (evt && evt.sourceId && (evt.sourceId.includes("geomet") || evt.sourceId.includes("seamarks"))) {
        return;
      }
    });

    map.on("mousemove", (e) => {
      const hud = document.getElementById("cursor-coords");
      if (hud) {
        hud.textContent = formatDmsPair(e.lngLat.lat, e.lngLat.lng);
      }
    });

    map.on("load", () => {
      // Record CARTO Dark Matter base layers so the user can toggle basemap visibility cleanly
      state.baseLayerIds = map.getStyle().layers.map((l) => l.id);

      registerTacticalIcons(map);

      // 1. OpenSeaMap Seamarks Overlay
      map.addSource("seamarks-source", {
        type: "raster",
        tiles: ["https://tiles.openseamap.org/seamark/{z}/{x}/{y}.png"],
        tileSize: 256,
        minzoom: 3,
        maxzoom: 18,
        attribution: "&copy; OpenSeaMap contributors",
      });

      map.addLayer({
        id: "seamarks-layer",
        type: "raster",
        source: "seamarks-source",
        layout: {
          visibility: document.getElementById("toggle-seamarks").checked ? "visible" : "none",
        },
        paint: {
          "raster-opacity": 0.88,
        },
      });

      // 2. 200 NM SAR Geodesic Range Rings (Turf.js)
      map.addSource("sar-rings-source", {
        type: "geojson",
        data: buildRangeRingsGeoJSON(),
      });

      const ringsVisible = document.getElementById("toggle-range-rings").checked ? "visible" : "none";

      map.addLayer({
        id: "sar-rings-fill",
        type: "fill",
        source: "sar-rings-source",
        layout: { visibility: ringsVisible },
        paint: {
          "fill-color": "#22d3ee",
          "fill-opacity": ["case", ["==", ["get", "selected"], 1], 0.09, 0.025],
        },
      });

      map.addLayer({
        id: "sar-rings-outline",
        type: "line",
        source: "sar-rings-source",
        layout: { visibility: ringsVisible },
        paint: {
          "line-color": ["case", ["==", ["get", "selected"], 1], "#22d3ee", "rgba(34, 211, 238, 0.34)"],
          "line-width": ["case", ["==", ["get", "selected"], 1], 1.8, 1.0],
          "line-dasharray": [4, 3],
        },
      });

      // 3. Selected Vessel Station-to-Position Tether
      map.addSource("selected-tether-source", {
        type: "geojson",
        data: buildSelectedTetherGeoJSON(),
      });

      map.addLayer({
        id: "selected-tether-layer",
        type: "line",
        source: "selected-tether-source",
        paint: {
          "line-color": "#38bdf8",
          "line-width": 1.5,
          "line-dasharray": [2, 2],
          "line-opacity": 0.75,
        },
      });

      // 4. 3-Tier Vessel Positions Source & Symbol Layers
      map.addSource("vessels-source", {
        type: "geojson",
        data: buildVesselsGeoJSON(),
      });

      // Selected vessel outer highlight ring
      map.addLayer({
        id: "vessel-selected-halo",
        type: "circle",
        source: "vessels-source",
        filter: ["==", ["get", "selected"], 1],
        paint: {
          "circle-radius": 18,
          "circle-color": "rgba(34, 211, 238, 0.14)",
          "circle-stroke-color": "#22d3ee",
          "circle-stroke-width": 2,
        },
      });

      // Tier 3: Home Port Fallback (> 7d or no AIS)
      map.addLayer({
        id: "vessels-fallback-layer",
        type: "symbol",
        source: "vessels-source",
        filter: ["==", ["get", "tier"], "fallback"],
        layout: {
          "icon-image": "icon-home-port",
          "icon-size": 0.92,
          "icon-allow-overlap": true,
          "icon-ignore-placement": true,
        },
      });

      // Tier 2: Last Known (2h - 7d)
      map.addLayer({
        id: "vessels-last-known-layer",
        type: "symbol",
        source: "vessels-source",
        filter: ["==", ["get", "tier"], "last_known"],
        layout: {
          "icon-image": "icon-last-known",
          "icon-size": 0.96,
          "icon-allow-overlap": true,
          "icon-ignore-placement": true,
        },
      });

      // Tier 1: Active / Recent (< 2h) - Rotated by COG
      map.addLayer({
        id: "vessels-active-layer",
        type: "symbol",
        source: "vessels-source",
        filter: ["==", ["get", "tier"], "active"],
        layout: {
          "icon-image": "icon-active-arrow",
          "icon-size": 0.96,
          "icon-rotate": ["get", "cog"],
          "icon-rotation-alignment": "map",
          "icon-allow-overlap": true,
          "icon-ignore-placement": true,
        },
      });

      state.mapLoaded = true;
      syncMapSources();
      bindMapInteractions(map);
    });
  }

  function bindMapInteractions(map) {
    const interactiveLayers = [
      "vessels-active-layer",
      "vessels-last-known-layer",
      "vessels-fallback-layer",
    ];

    state.hoverPopup = new window.maplibregl.Popup({
      closeButton: false,
      closeOnClick: false,
      offset: 14,
      className: "tactical-popup",
    });

    for (const layerId of interactiveLayers) {
      map.on("mouseenter", layerId, (e) => {
        map.getCanvas().style.cursor = "pointer";
        const feat = e.features && e.features[0];
        if (!feat) return;

        const p = feat.properties;
        const coords = feat.geometry.coordinates.slice();

        let tierTitle = "";
        let detailHtml = "";
        if (p.tier === "active") {
          tierTitle = "Active / Recent";
          detailHtml = `SOG: <strong>${escapeHtml(p.sog)} kt</strong> &bull; COG: <strong>${Math.round(Number(p.cog))}&deg;</strong><br/>Updated ${escapeHtml(p.ageText)}`;
        } else if (p.tier === "last_known") {
          tierTitle = "Last Known Position";
          detailHtml = `${escapeHtml(p.tierLabel)}<br/>Home: ${escapeHtml(p.home_port)}`;
        } else {
          tierTitle = "Home Port (No recent AIS)";
          detailHtml = `Station: ${escapeHtml(p.home_port)}`;
        }

        const html = `
          <div class="popup-title">${escapeHtml(p.name)}</div>
          <div class="popup-tier ${escapeHtml(p.tier)}">${tierTitle}</div>
          <div class="popup-detail">${detailHtml}</div>
        `;

        state.hoverPopup.setLngLat(coords).setHTML(html).addTo(map);
      });

      map.on("mouseleave", layerId, () => {
        map.getCanvas().style.cursor = "";
        state.hoverPopup.remove();
      });

      map.on("click", layerId, (e) => {
        const feat = e.features && e.features[0];
        if (!feat) return;
        selectVessel(feat.properties.id, true);
      });
    }
  }

  function fitToVisibleVessels() {
    if (!state.map) return;
    if (state.filtered.length === 0) {
      state.map.fitBounds(REGION_BOUNDS[state.regionFilter] || REGION_BOUNDS.All, { padding: 48 });
      return;
    }
    if (state.filtered.length === 1) {
      const only = state.filtered[0];
      state.map.flyTo({ center: [only.displayLon, only.displayLat], zoom: 8 });
      return;
    }
    const bounds = new window.maplibregl.LngLatBounds();
    for (const v of state.filtered) {
      bounds.extend([v.displayLon, v.displayLat]);
    }
    state.map.fitBounds(bounds, {
      padding: { top: 60, bottom: 70, left: 60, right: 60 },
      maxZoom: 9,
      duration: 900,
    });
  }

  /* =========================================================
     DOM Controls & Event Wiring
     ========================================================= */

  function syncFilterButtonsUI() {
    document.querySelectorAll("#region-filters .seg-btn").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.region === state.regionFilter);
    });
    document.querySelectorAll("#status-filters .seg-btn").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.status === state.statusFilter);
    });
    document.querySelectorAll(".kpi-pill[data-status-shortcut]").forEach((pill) => {
      const target = pill.dataset.statusShortcut;
      pill.classList.toggle("is-filtered", target !== "all" && target === state.statusFilter);
    });
  }

  function bindUIEvents() {
    // Search input
    const searchInput = document.getElementById("search-input");
    const clearBtn = document.getElementById("clear-search-btn");

    searchInput.addEventListener("input", () => {
      state.searchQuery = searchInput.value;
      clearBtn.hidden = !state.searchQuery;
      applyFilters();
    });

    clearBtn.addEventListener("click", () => {
      searchInput.value = "";
      state.searchQuery = "";
      clearBtn.hidden = true;
      applyFilters();
      searchInput.focus();
    });

    // Region segmented buttons
    document.getElementById("region-filters").addEventListener("click", (e) => {
      const btn = e.target.closest(".seg-btn");
      if (!btn) return;
      state.regionFilter = btn.dataset.region;
      syncFilterButtonsUI();
      applyFilters();
      if (state.map && REGION_BOUNDS[state.regionFilter]) {
        state.map.fitBounds(REGION_BOUNDS[state.regionFilter], {
          padding: { top: 55, bottom: 65, left: 55, right: 55 },
          duration: 950,
        });
      }
    });

    // Status segmented buttons
    document.getElementById("status-filters").addEventListener("click", (e) => {
      const btn = e.target.closest(".seg-btn");
      if (!btn) return;
      state.statusFilter = btn.dataset.status;
      syncFilterButtonsUI();
      applyFilters();
    });

    // Top summary bar KPI shortcuts
    document.querySelectorAll(".kpi-pill[data-status-shortcut]").forEach((pill) => {
      pill.addEventListener("click", () => {
        const chosen = pill.dataset.statusShortcut;
        state.statusFilter = state.statusFilter === chosen && chosen !== "all" ? "all" : chosen;
        syncFilterButtonsUI();
        applyFilters();
      });
    });

    // Roster click -> select vessel
    document.getElementById("vessel-list").addEventListener("click", (e) => {
      const row = e.target.closest(".vessel-item");
      if (!row) return;
      selectVessel(row.dataset.vesselId, true);
    });

    // Fit bounds button
    document.getElementById("fit-bounds-btn").addEventListener("click", fitToVisibleVessels);

    // Detail drawer actions
    document.getElementById("close-drawer-btn").addEventListener("click", closeDetailDrawer);
    document.getElementById("center-vessel-btn").addEventListener("click", () => {
      if (!state.selectedVesselId || !state.map) return;
      const v = state.enriched.find((x) => x.id === state.selectedVesselId);
      if (v) {
        state.map.flyTo({ center: [v.displayLon, v.displayLat], zoom: 9.5, speed: 1.3 });
      }
    });

    // Escape key closes drawer
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && state.selectedVesselId) {
        closeDetailDrawer();
      }
    });

    // Mobile sidebar menu toggle
    const mobileBtn = document.getElementById("mobile-menu-btn");
    const sidebar = document.getElementById("sidebar");
    mobileBtn.addEventListener("click", () => {
      const isOpen = sidebar.classList.toggle("mobile-open");
      mobileBtn.setAttribute("aria-expanded", String(isOpen));
    });

    // Map layer toggles
    document.getElementById("toggle-basemap").addEventListener("change", (e) => {
      if (!state.map || !state.mapLoaded) return;
      const vis = e.target.checked ? "visible" : "none";
      for (const id of state.baseLayerIds) {
        if (state.map.getLayer(id)) {
          state.map.setLayoutProperty(id, "visibility", vis);
        }
      }
    });

    document.getElementById("toggle-range-rings").addEventListener("change", (e) => {
      if (!state.map || !state.mapLoaded) return;
      const vis = e.target.checked ? "visible" : "none";
      for (const id of ["sar-rings-fill", "sar-rings-outline"]) {
        if (state.map.getLayer(id)) {
          state.map.setLayoutProperty(id, "visibility", vis);
        }
      }
    });

    document.getElementById("toggle-seamarks").addEventListener("change", (e) => {
      if (!state.map || !state.mapLoaded) return;
      if (state.map.getLayer("seamarks-layer")) {
        state.map.setLayoutProperty("seamarks-layer", "visibility", e.target.checked ? "visible" : "none");
      }
    });

    document.getElementById("toggle-wind").addEventListener("change", (e) => {
      toggleGeometOverlay("wind", e.target.checked, GEOMET_WIND_CANDIDATES, "wind-layer-status", e.target);
    });

    document.getElementById("toggle-ice").addEventListener("change", (e) => {
      toggleGeometOverlay("ice", e.target.checked, GEOMET_ICE_CANDIDATES, "ice-layer-status", e.target);
    });
  }

  /* =========================================================
     Bootstrap
     ========================================================= */

  async function bootstrap() {
    bindUIEvents();
    initMap();
    try {
      await loadData(false);
    } catch (err) {
      console.error("Failed to initialize CCG Cape Class fleet data:", err);
      document.getElementById("vessel-list").innerHTML =
        `<div class="empty-roster">Unable to load fleet registry. Serve via HTTP (e.g. start.ps1 or GitHub Pages).</div>`;
    }

    // Periodically refresh positions cache & relative timestamps
    setInterval(() => {
      loadData(true).catch(() => {});
    }, REFRESH_INTERVAL_MS);

    setInterval(() => {
      if (state.vessels.length > 0) {
        recomputeFleetState();
      }
    }, 60 * 1000);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bootstrap);
  } else {
    bootstrap();
  }
})();
