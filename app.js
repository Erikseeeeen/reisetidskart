const OSLO_S = L.latLng(59.9111, 10.7528);
const BOUNDS = L.latLngBounds([59.66, 10.2], [60.17, 11.3]);
const API_URL = document.querySelector('meta[name="travel-time-api"]').content;
const WIDTH = 320;
const HEIGHT = 240;
const MAX_MINUTES = 60;
const TIME_STEPS = [0, 10, 20, 30, 40, 50, 60];

const COLOR_SCALES = {
  viridis: ["#440154", "#414487", "#2a788e", "#22a884", "#5ec962", "#aadc32", "#fde725"],
  magma: ["#000004", "#1c1044", "#4f127b", "#812581", "#a72370", "#d64868", "#fb8861"],
  inferno: ["#000004", "#1f0c48", "#550f6d", "#88226a", "#ad3358", "#e66639", "#fcffa4"],
  turbo: ["#30123b", "#255ab5", "#1ea2a5", "#48ce5a", "#96e73e", "#f0a022", "#f54800"],
  cividis: ["#00204c", "#00396d", "#275277", "#5c6b70", "#7c7a68", "#bda654", "#ead743"],
  blueRed: ["#313695", "#4575b4", "#74add1", "#abd9e9", "#e8baa0", "#e36f59", "#d73027"],
  greyscale: ["#141414", "#3a3a3a", "#5f5f5f", "#868686", "#a4a4a4", "#d0d0d0", "#f5f5f5"],
};

const paletteButtons = [...document.querySelectorAll(".palette-option")];
const legend = document.querySelector(".legend-gradient");
const legendTicks = document.querySelector(".legend-ticks");
const paletteName = document.querySelector("#palette-name");
const startCoordinates = document.querySelector("#start-coordinates");
const fieldValue = document.querySelector("#field-value");
const engineStatus = document.querySelector("#engine-status");
const requestedScale = new URLSearchParams(location.search).get("scale");
let activeScale = COLOR_SCALES[requestedScale] ? requestedScale : "viridis";
let startPoint = OSLO_S;
let currentGrid;
let activeRequest;

const map = L.map("map", {
  zoomControl: false,
  minZoom: 9,
  maxBounds: BOUNDS.pad(0.18),
  maxBoundsViscosity: 0.8,
}).setView(OSLO_S, 11);

L.control.zoom({ position: "bottomright" }).addTo(map);
L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
  className: "greyscale-tiles",
}).addTo(map);

const blank = document.createElement("canvas");
blank.width = blank.height = 1;
const heatLayer = L.imageOverlay(blank.toDataURL(), BOUNDS, {
  opacity: 0.8,
  interactive: false,
}).addTo(map);

const marker = L.marker(startPoint, {
  draggable: true,
  icon: L.divIcon({
    className: "start-marker",
    html: '<span class="start-marker__pin" aria-hidden="true"></span>',
    iconSize: [28, 28],
    iconAnchor: [8, 25],
  }),
}).addTo(map).bindTooltip("Startpunkt");

function rgb(hex) {
  return [1, 3, 5].map((index) => parseInt(hex.slice(index, index + 2), 16));
}

function colourAt(minutes) {
  const colours = COLOR_SCALES[activeScale];
  const bucket = Math.min(Math.floor(Math.max(0, minutes) / 10), colours.length - 2);
  return rgb(colours[bucket]);
}

function gradient(colours) {
  const stops = colours.slice(0, -1).flatMap((colour, index) => {
    const start = (TIME_STEPS[index] / MAX_MINUTES) * 100;
    const end = (TIME_STEPS[index + 1] / MAX_MINUTES) * 100;
    return [`${colour} ${start}%`, `${colour} ${end}%`];
  });
  return `linear-gradient(90deg, ${stops.join(", ")})`;
}

function renderField(minutes) {
  const canvas = document.createElement("canvas");
  canvas.width = WIDTH;
  canvas.height = HEIGHT;
  const context = canvas.getContext("2d");
  const image = context.createImageData(WIDTH, HEIGHT);

  minutes.forEach((minute, index) => {
    if (minute > MAX_MINUTES) return;
    image.data.set([...colourAt(minute), 255], index * 4);
  });

  context.putImageData(image, 0, 0);
  heatLayer.setUrl(canvas.toDataURL("image/png"));
  currentGrid = minutes;
}

function updatePalette() {
  legend.style.background = gradient(COLOR_SCALES[activeScale]);
  paletteButtons.forEach((button) => {
    const selected = button.dataset.scale === activeScale;
    button.classList.toggle("is-active", selected);
    button.setAttribute("aria-checked", String(selected));
    button.tabIndex = selected ? 0 : -1;
    if (selected) paletteName.textContent = button.textContent.trim();
  });
  if (currentGrid) renderField(currentGrid);
}

function sampleGrid(lat, lng) {
  if (!currentGrid || !BOUNDS.contains([lat, lng])) return null;
  const x = Math.min(WIDTH - 1, Math.floor(
    (lng - BOUNDS.getWest()) / (BOUNDS.getEast() - BOUNDS.getWest()) * WIDTH,
  ));
  const y = Math.min(HEIGHT - 1, Math.floor(
    (BOUNDS.getNorth() - lat) / (BOUNDS.getNorth() - BOUNDS.getSouth()) * HEIGHT,
  ));
  return currentGrid[y * WIDTH + x];
}

function setStatus(text, state) {
  engineStatus.textContent = text;
  engineStatus.dataset.state = state;
}

async function requestTravelTimes() {
  activeRequest?.abort();
  const controller = new AbortController();
  activeRequest = controller;
  fieldValue.textContent = "Beregner …";
  setStatus("Beregner", "loading");

  const request = {
    origin: { lat: startPoint.lat, lng: startPoint.lng },
    bounds: { south: BOUNDS.getSouth(), west: BOUNDS.getWest(), north: BOUNDS.getNorth(), east: BOUNDS.getEast() },
    width: WIDTH,
    height: HEIGHT,
  };

  try {
    const response = await fetch(`${API_URL}/travel-times`, {
      method: "POST",
      body: JSON.stringify(request),
      signal: controller.signal,
    });
    const payload = await response.json();
    if (!response.ok || payload.minutes?.length !== WIDTH * HEIGHT) {
      throw new Error(payload.error ?? "Ugyldig svar fra motoren");
    }
    renderField(payload.minutes);
    fieldValue.textContent = "Flytt pekeren over kartet";
    setStatus("Tilkoblet", "connected");
  } catch (error) {
    if (error.name === "AbortError") return;
    console.error(error);
    fieldValue.textContent = "Start Python-tjenesten på port 8001";
    setStatus("Frakoblet", "error");
  } finally {
    if (activeRequest === controller) activeRequest = null;
  }
}

function setStartPoint(point, label) {
  startPoint = L.latLng(point);
  marker.setLatLng(startPoint);
  startCoordinates.textContent = label ?? `${startPoint.lat.toFixed(4)}° N, ${startPoint.lng.toFixed(4)}° Ø`;
  requestTravelTimes();
}

legendTicks.replaceChildren(...TIME_STEPS.map((minute) => {
  const tick = document.createElement("span");
  tick.textContent = minute;
  return tick;
}));

paletteButtons.forEach((button) => {
  button.querySelector(".palette-swatch").style.background = gradient(COLOR_SCALES[button.dataset.scale]);
  button.addEventListener("click", () => {
    activeScale = button.dataset.scale;
    updatePalette();
  });
});

map.on("click", (event) => setStartPoint(event.latlng));
map.on("mousemove", (event) => {
  const minutes = sampleGrid(event.latlng.lat, event.latlng.lng);
  if (minutes !== null) {
    fieldValue.textContent = minutes <= MAX_MINUTES ? `Omtrent ${Math.round(minutes)} min` : "Mer enn 60 min";
  }
});
marker.on("dragend", () => setStartPoint(marker.getLatLng()));
document.querySelector("#reset-start").addEventListener("click", () => {
  setStartPoint(OSLO_S, "Oslo S");
  map.flyTo(OSLO_S, 11);
});

updatePalette();
requestTravelTimes();
