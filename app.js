const OSLO_S = L.latLng(59.9111, 10.7528);
const NORWAY_BOUNDS = L.latLngBounds([57.5, 3.5], [71.6, 32.0]);
const API_URL = document.querySelector('meta[name="travel-time-api"]').content;
const WIDTH = 320;
const HEIGHT = 240;
const MAX_MINUTES = 60;
const TIME_STEPS = [0, 10, 20, 30, 40, 50, 60];
const CLIENT_ID = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`;
const MOVE_DEBOUNCE_MS = 300;

const VIRIDIS = ["#440154", "#414487", "#2a788e", "#22a884", "#5ec962", "#aadc32", "#fde725"];

const legendTicks = document.querySelector(".legend-ticks");
const startCoordinates = document.querySelector("#start-coordinates");
const fieldValue = document.querySelector("#field-value");
const engineStatus = document.querySelector("#engine-status");
const transportModes = [...document.querySelectorAll('input[name="transport-mode"]')];
let startPoint = OSLO_S;
let currentGrid;
let currentBounds;
let activeRequest;
let requestSequence = 0;
let moveRequestTimer;

const map = L.map("map", {
  zoomControl: false,
  minZoom: 4,
  maxBounds: NORWAY_BOUNDS.pad(0.12),
  maxBoundsViscosity: 0.8,
}).setView(OSLO_S, 11);

L.control.zoom({ position: "bottomright" }).addTo(map);
L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
  className: "greyscale-tiles",
}).addTo(map);

const heatCanvas = document.createElement("canvas");
heatCanvas.width = WIDTH;
heatCanvas.height = HEIGHT;
const heatContext = heatCanvas.getContext("2d");
const heatLayer = L.imageOverlay(heatCanvas.toDataURL(), map.getBounds(), {
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

function morningDepartureTime() {
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat("en", {
      timeZone: "Europe/Oslo",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).formatToParts().map(({ type, value }) => [type, value]),
  );
  return `${parts.year}-${parts.month}-${parts.day}T08:00:00`;
}

function renderField(minutes, bounds) {
  const colours = VIRIDIS.map(rgb);
  const image = heatContext.createImageData(WIDTH, HEIGHT);

  minutes.forEach((minute, index) => {
    if (minute > MAX_MINUTES) return;
    const bucket = Math.min(Math.floor(Math.max(0, minute) / 10), colours.length - 2);
    image.data.set([...colours[bucket], 255], index * 4);
  });

  heatContext.putImageData(image, 0, 0);
  heatLayer.setUrl(heatCanvas.toDataURL("image/png"));
  heatLayer.setBounds(bounds);
  currentGrid = minutes;
  currentBounds = bounds;
}

function sampleGrid(lat, lng) {
  if (!currentGrid || !currentBounds?.contains([lat, lng])) return null;
  const x = Math.min(WIDTH - 1, Math.floor(
    (lng - currentBounds.getWest()) / (currentBounds.getEast() - currentBounds.getWest()) * WIDTH,
  ));
  const y = Math.min(HEIGHT - 1, Math.floor(
    (currentBounds.getNorth() - lat) / (currentBounds.getNorth() - currentBounds.getSouth()) * HEIGHT,
  ));
  return currentGrid[y * WIDTH + x];
}

function setStatus(text, state) {
  engineStatus.textContent = text;
  engineStatus.dataset.state = state;
}

async function requestTravelTimes() {
  if (map.getZoom() < 8) {
    activeRequest?.abort();
    currentGrid = undefined;
    currentBounds = undefined;
    heatLayer.setOpacity(0);
    fieldValue.textContent = "Klikk nær en by for å beregne";
    return;
  }

  activeRequest?.abort();
  const controller = new AbortController();
  activeRequest = controller;
  fieldValue.textContent = "Beregner …";
  setStatus("Beregner", "loading");

  const requestBounds = map.getBounds().pad(0.04);
  const request = {
    client_id: CLIENT_ID,
    request_id: ++requestSequence,
    origin: { lat: startPoint.lat, lng: startPoint.lng },
    bounds: {
      south: requestBounds.getSouth(),
      west: requestBounds.getWest(),
      north: requestBounds.getNorth(),
      east: requestBounds.getEast(),
    },
    width: WIDTH,
    height: HEIGHT,
    mode: transportModes.find((input) => input.checked).value,
    departure_time: morningDepartureTime(),
  };

  try {
    const response = await fetch(`${API_URL}/travel-times`, {
      method: "POST",
      body: JSON.stringify(request),
      signal: controller.signal,
    });
    const payload = await response.json();
    if (activeRequest !== controller) return;
    if (!response.ok || payload.minutes?.length !== WIDTH * HEIGHT) {
      throw new Error(payload.error ?? "Ugyldig svar fra motoren");
    }
    heatLayer.setOpacity(0.8);
    renderField(payload.minutes, requestBounds);
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

function scheduleTravelTimes() {
  clearTimeout(moveRequestTimer);
  moveRequestTimer = setTimeout(requestTravelTimes, MOVE_DEBOUNCE_MS);
}

function setStartPoint(point, label) {
  startPoint = L.latLng(point);
  marker.setLatLng(startPoint);
  startCoordinates.textContent = label ?? `${startPoint.lat.toFixed(4)}° N, ${startPoint.lng.toFixed(4)}° Ø`;
  if (map.getZoom() < 8) {
    map.setView(startPoint, 9);
  } else {
    requestTravelTimes();
  }
}

legendTicks.replaceChildren(...TIME_STEPS.map((minute) => {
  const tick = document.createElement("span");
  tick.textContent = minute;
  return tick;
}));

map.on("click", (event) => setStartPoint(event.latlng));
map.on("moveend", scheduleTravelTimes);
map.on("mousemove", (event) => {
  const minutes = sampleGrid(event.latlng.lat, event.latlng.lng);
  if (minutes !== null) {
    fieldValue.textContent = minutes <= MAX_MINUTES ? `Omtrent ${Math.round(minutes)} min` : "Mer enn 60 min";
  }
});
marker.on("dragend", () => setStartPoint(marker.getLatLng()));
transportModes.forEach((input) => input.addEventListener("change", requestTravelTimes));

requestTravelTimes();
