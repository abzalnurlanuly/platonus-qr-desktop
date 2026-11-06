"use strict";

const byId = (id) => document.getElementById(id);
const loginForm = byId("login-form");
const image = byId("image");
const video = byId("video");
const log = byId("diagnostics");
const logOutput = byId("diagnostics-output");
const maxImageBytes = 8 * 1024 * 1024;
const tokenPattern = /^[A-Za-z0-9_-]{32,128}$/;
const languageKey = "platonus-qr-language";

function languagePreference() {
  let saved;
  try { saved = localStorage.getItem(languageKey); } catch { /* Storage can be disabled. */ }
  if (Object.hasOwn(uiStrings, saved)) return saved;
  const host = document.body.dataset.hostLanguage;
  if (Object.hasOwn(uiStrings, host)) return host;
  return navigator.language.toLowerCase().startsWith("ru") ? "ru" : "en";
}

let token = location.hash.slice(1);
if (location.hash) history.replaceState(null, "", location.pathname + location.search);
if (!tokenPattern.test(token)) token = "";

const state = {
  language: languagePreference(),
  account: null,
  busy: false,
  identityRequired: false,
  qr: null,
  source: null,
  locationRequired: false,
  locationChecking: false,
  position: null,
  locating: false,
  submitted: false,
  status: null
};

let qrRevision = 0;
let positionRevision = 0;
let diagnosticsRevision = 0;
let locationMap;
let cameraRevision = 0;
let cameraOpen = false;
let cameraStream;
let cameraTimer;
let cameraAbort;
const frame = document.createElement("canvas");

function text(key, values = {}) {
  const strings = uiStrings[state.language];
  const phrase = key.startsWith("errors.") ? strings.errors[key.slice(7)] : strings[key];
  return (phrase || strings.requestFailed).replace(/\{(\w+)\}/g, (_, name) => String(values[name] ?? ""));
}

function setLanguage(language) {
  state.language = language;
  document.documentElement.lang = language;
  document.querySelectorAll("[data-text]").forEach((node) => { node.textContent = text(node.dataset.text); });
  document.querySelectorAll("[data-label]").forEach((node) => {
    node.setAttribute("aria-label", text(node.dataset.label));
  });
  document.querySelectorAll("[data-language]").forEach((button) => {
    button.setAttribute("aria-pressed", String(button.dataset.language === language));
  });
  render();
}

function showStatus(key, kind = "", values) {
  state.status = key ? { key, kind, values } : null;
  renderStatus();
}

function renderStatus() {
  const status = byId("status");
  status.hidden = !state.status;
  status.textContent = state.status ? text(state.status.key, state.status.values) : "";
  status.dataset.kind = state.status?.kind || "";
}

function render() {
  const signedIn = !!state.account;
  byId("access-section").hidden = !!token;
  byId("login-section").hidden = !token || signedIn;
  byId("attendance-section").hidden = !token || !signedIn;
  log.hidden = !token;
  byId("student-name").textContent = state.account?.display_name || text("unknownStudent");
  byId("identity-field").hidden = !state.identityRequired;
  byId("identity").required = state.identityRequired;
  byId("login-button").disabled = state.busy;
  byId("logout-button").disabled = state.busy;
  byId("image-button").disabled = state.busy || state.locationChecking;
  byId("camera-button").disabled = state.busy || state.locationChecking || cameraOpen;
  byId("camera-panel").hidden = !cameraOpen;
  byId("qr-summary").hidden = !state.qr;
  byId("clear-qr").disabled = state.busy;
  byId("location-section").hidden = !state.qr || !state.locationRequired;
  byId("device-location").disabled = state.locating || state.busy;
  byId("open-map").disabled = state.busy;
  byId("submit-button").disabled = !state.qr || state.busy || state.locationChecking ||
    state.submitted || (state.locationRequired && !state.position);

  if (state.qr) {
    byId("qr-type").textContent = text(state.qr.action === "event" ? "eventCode" : "classCode");
    byId("qr-source").textContent = text(state.source ? "imageSource" : "cameraSource", { name: state.source });
  }

  const coordinates = byId("coordinates");
  coordinates.hidden = !state.position && !state.locating;
  if (state.locating) coordinates.textContent = text("locating");
  else if (state.position) {
    coordinates.textContent = text(state.position.source === "device" ? "deviceCoordinates" : "mapCoordinates", {
      latitude: state.position.latitude.toFixed(6),
      longitude: state.position.longitude.toFixed(6)
    });
  }
  renderStatus();
}

function apiError(code, retryable = false) {
  return Object.assign(new Error(code), { code, retryable });
}

function errorKey(error) {
  const code = error?.code;
  if (Object.hasOwn(uiStrings[state.language].errors, code)) return "errors." + code;
  if (code === "networkFailed" || code === "readFailed") return code;
  return "requestFailed";
}

function clearSession() {
  closeCamera();
  clearQr();
  state.account = null;
  state.identityRequired = false;
  loginForm.reset();
  diagnosticsRevision += 1;
  logOutput.textContent = "";
}

async function request(path, { method = "POST", json, body, headers = {}, signal } = {}) {
  const sentToken = token;
  const requestHeaders = { ...headers, Authorization: "Bearer " + sentToken };
  if (json !== undefined) {
    requestHeaders["Content-Type"] = "application/json";
    body = JSON.stringify(json);
  }
  let response;
  try {
    response = await fetch("/api/v1/" + path, {
      method, body, signal, headers: requestHeaders,
      cache: "no-store", credentials: "omit", redirect: "error"
    });
  } catch (error) {
    if (signal?.aborted) throw error;
    throw apiError("networkFailed");
  }
  if (response.status === 204) return {};
  let data;
  try { data = await response.json(); } catch {
    throw apiError(response.status === 413 ? "request_too_large" : "readFailed");
  }
  if (!response.ok) {
    const error = data.error || {};
    if (token === sentToken && response.status === 401 && error.code === "unauthorized") {
      clearSession();
      token = "";
      log.open = false;
      render();
    } else if (state.account && ["session_expired", "not_authenticated"].includes(error.code)) {
      clearSession();
      render();
    }
    throw apiError(error.code || "requestFailed", error.retryable === true);
  }
  return data;
}

async function refreshLog() {
  if (!token || !log.open) return;
  const revision = ++diagnosticsRevision;
  try {
    const { entries } = await request("logs", { method: "GET" });
    if (revision !== diagnosticsRevision || !log.open) return;
    logOutput.textContent = Array.isArray(entries) && entries.length
      ? entries.map((entry) => JSON.stringify(entry)).join("\n") : text("noLogs");
  } catch {
    if (revision === diagnosticsRevision && log.open) logOutput.textContent = text("logFailed");
  }
}

async function refreshAccount() {
  if (!token) return;
  state.busy = true;
  showStatus("connecting");
  render();
  try {
    const account = await request("account", { method: "GET" });
    state.account = account.authenticated ? account : null;
    showStatus(null);
  } catch (error) {
    showStatus(errorKey(error), "error");
  } finally {
    state.busy = false;
    render();
  }
}

byId("access-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const input = byId("access-key");
  const value = input.value.trim();
  input.value = "";
  if (!tokenPattern.test(value)) {
    showStatus("errors.unauthorized", "error");
    return;
  }
  token = value;
  void refreshAccount();
});

loginForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!token || state.busy) return;
  const password = byId("password");
  const credentials = {
    university_url: byId("university-url").value.trim(),
    username: byId("username").value.trim(),
    password: password.value,
    language: state.language
  };
  if (state.identityRequired) credentials.iin = byId("identity").value.trim();
  password.value = "";
  state.busy = true;
  showStatus("signingIn");
  render();
  try {
    const account = await request("login", { json: credentials });
    if (!account.authenticated) throw apiError("not_authenticated");
    state.account = account;
    state.identityRequired = false;
    loginForm.reset();
    showStatus(null);
    render();
    byId("student-name").focus();
  } catch (error) {
    if (error.code === "additional_identity_required") state.identityRequired = true;
    showStatus(errorKey(error), "error");
    render();
    (error.code === "additional_identity_required" ? byId("identity") : password).focus();
  } finally {
    credentials.password = "";
    credentials.iin = "";
    state.busy = false;
    render();
    void refreshLog();
  }
});

byId("university-url").addEventListener("input", () => {
  if (!state.identityRequired) return;
  state.identityRequired = false;
  byId("identity").value = "";
  render();
});

byId("logout-button").addEventListener("click", async () => {
  if (!state.account || state.busy) return;
  closeCamera();
  state.busy = true;
  showStatus("signingOut");
  render();
  try {
    await request("logout");
    clearSession();
    showStatus(null);
    render();
    byId("university-url").focus();
  } catch (error) {
    showStatus(errorKey(error), "error");
  } finally {
    state.busy = false;
    render();
    void refreshLog();
  }
});

function closeMap() {
  byId("map-panel").hidden = true;
  byId("open-map").setAttribute("aria-expanded", "false");
  locationMap?.close();
}

function clearQr() {
  qrRevision += 1;
  positionRevision += 1;
  state.qr = null;
  state.source = null;
  state.locationRequired = false;
  state.locationChecking = false;
  state.position = null;
  state.locating = false;
  state.submitted = false;
  closeMap();
  locationMap?.select(null);
}

async function acceptQr(qr, source) {
  if (typeof qr?.raw !== "string" || !["journal", "event"].includes(qr.action)) throw apiError("invalid_qr");
  clearQr();
  state.qr = qr;
  state.source = source;
  showStatus(null);
  render();
  if (qr.action !== "journal" || !qr.location_flag) return;
  const revision = qrRevision;
  state.locationChecking = true;
  showStatus("checkingLocation");
  render();
  try {
    const { required } = await request("location", { json: { qr: qr.raw } });
    if (revision !== qrRevision) return;
    state.locationRequired = required === true;
    showStatus(null);
  } catch (error) {
    if (revision === qrRevision || !state.account) showStatus(errorKey(error), "error");
  } finally {
    if (revision === qrRevision) {
      state.locationChecking = false;
      render();
    }
    void refreshLog();
  }
}

byId("clear-qr").addEventListener("click", () => {
  clearQr();
  showStatus(null);
  render();
});

function uploadBody(blob, filename) {
  const body = new FormData();
  body.append("image", blob, filename);
  return body;
}

byId("image-button").addEventListener("click", () => image.click());
image.addEventListener("change", async () => {
  const file = image.files[0];
  image.value = "";
  if (!file || !state.account || state.busy) return;
  closeCamera();
  clearQr();
  if (file.size > maxImageBytes) {
    showStatus("errors.image_too_large", "error");
    render();
    return;
  }
  state.busy = true;
  showStatus("checkingImage");
  render();
  try {
    const qr = await request("decode", { body: uploadBody(file, "upload") });
    await acceptQr(qr, file.name);
  } catch (error) {
    showStatus(errorKey(error), "error");
  } finally {
    state.busy = false;
    render();
    void refreshLog();
  }
});

function closeCamera() {
  cameraRevision += 1;
  clearTimeout(cameraTimer);
  cameraAbort?.abort();
  cameraAbort = null;
  cameraStream?.getTracks().forEach((track) => track.stop());
  cameraStream = null;
  video.srcObject = null;
  cameraOpen = false;
  frame.width = 0;
  frame.height = 0;
}

async function scanFrame(revision) {
  if (!cameraOpen || revision !== cameraRevision) return;
  if (video.readyState >= 2 && video.videoWidth && video.videoHeight) {
    const scale = Math.min(1, 960 / Math.max(video.videoWidth, video.videoHeight));
    const width = Math.round(video.videoWidth * scale);
    const height = Math.round(video.videoHeight * scale);
    if (frame.width !== width || frame.height !== height) {
      frame.width = width;
      frame.height = height;
    }
    let blob;
    try {
      frame.getContext("2d").drawImage(video, 0, 0, width, height);
      blob = await new Promise((resolve) => frame.toBlob(resolve, "image/jpeg", .84));
    } catch {
      if (revision !== cameraRevision) return;
      closeCamera();
      showStatus("cameraDenied", "error");
      render();
      return;
    }
    if (!cameraOpen || revision !== cameraRevision) return;
    if (blob) {
      const controller = new AbortController();
      cameraAbort = controller;
      try {
        const qr = await request("decode", {
          body: uploadBody(blob, "camera.jpg"),
          headers: { "X-Scan-Mode": "camera" },
          signal: controller.signal
        });
        if (revision !== cameraRevision) return;
        closeCamera();
        await acceptQr(qr, null);
        return;
      } catch (error) {
        if (revision !== cameraRevision) {
          if (!state.account) showStatus(errorKey(error), "error");
          return;
        }
        if (!["invalid_qr", "busy"].includes(error.code)) {
          closeCamera();
          showStatus(errorKey(error), "error");
          render();
          return;
        }
      } finally {
        if (cameraAbort === controller) cameraAbort = null;
      }
    }
  }
  cameraTimer = setTimeout(() => void scanFrame(revision), 550);
}

byId("camera-button").addEventListener("click", async () => {
  if (!state.account || state.busy || cameraOpen) return;
  if (!navigator.mediaDevices?.getUserMedia) {
    showStatus("cameraDenied", "error");
    return;
  }
  clearQr();
  const revision = ++cameraRevision;
  cameraOpen = true;
  showStatus("cameraOpening");
  render();
  try {
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: false,
      video: { facingMode: { ideal: "environment" }, width: { ideal: 960 }, height: { ideal: 720 } }
    });
    if (revision !== cameraRevision) {
      stream.getTracks().forEach((track) => track.stop());
      return;
    }
    cameraStream = stream;
    stream.getVideoTracks().forEach((track) => track.addEventListener("ended", () => {
      if (revision !== cameraRevision) return;
      closeCamera();
      showStatus("cameraDenied", "error");
      render();
    }, { once: true }));
    video.srcObject = stream;
    await video.play();
    if (revision !== cameraRevision) return;
    showStatus("cameraScanning");
    void scanFrame(revision);
  } catch {
    if (revision !== cameraRevision) return;
    closeCamera();
    showStatus("cameraDenied", "error");
    render();
  }
});

byId("camera-close").addEventListener("click", () => {
  closeCamera();
  showStatus(null);
  render();
  byId("camera-button").focus();
});

byId("device-location").addEventListener("click", () => {
  if (!state.locationRequired || state.locating || state.busy) return;
  if (!navigator.geolocation) {
    showStatus("deviceUnavailable", "error");
    return;
  }
  const revision = ++positionRevision;
  state.locating = true;
  showStatus(null);
  render();
  navigator.geolocation.getCurrentPosition((position) => {
    if (revision !== positionRevision) return;
    state.position = { latitude: position.coords.latitude, longitude: position.coords.longitude, source: "device" };
    state.locating = false;
    locationMap?.select(null);
    render();
  }, () => {
    if (revision !== positionRevision) return;
    state.locating = false;
    showStatus("deviceUnavailable", "error");
    render();
  }, { enableHighAccuracy: true, maximumAge: 0, timeout: 15000 });
});

byId("open-map").addEventListener("click", () => {
  if (!state.locationRequired || state.busy) return;
  const panel = byId("map-panel");
  if (!panel.hidden) {
    closeMap();
    return;
  }
  locationMap ??= new AttendanceMap((position) => {
    if (!state.locationRequired || state.busy) return;
    positionRevision += 1;
    state.locating = false;
    state.position = { ...position, source: "map" };
    showStatus(null);
    render();
  });
  panel.hidden = false;
  byId("open-map").setAttribute("aria-expanded", "true");
  locationMap.open();
  byId("map").focus();
});

byId("submit-button").addEventListener("click", async () => {
  if (!state.account || !state.qr || state.busy || state.submitted || state.locationChecking) return;
  if (state.locationRequired && !state.position) return;
  state.busy = true;
  state.submitted = true;
  showStatus("marking");
  render();
  const json = { qr: state.qr.raw };
  if (state.locationRequired && state.position) {
    const { latitude, longitude } = state.position;
    json.coordinates = { latitude, longitude };
  }
  try {
    const result = await request("attendance", { json });
    if (result.success !== true) {
      showStatus("attendanceRejected", "error");
    } else if (state.account?.display_name) {
      showStatus("attendanceMarked", "success", { name: state.account.display_name });
    } else {
      showStatus("attendanceMarkedUnknown", "success");
    }
  } catch (error) {
    if (error.code === "location_required" && state.qr) {
      state.locationRequired = true;
      state.submitted = false;
    } else if (error.retryable) {
      state.submitted = false;
    }
    const unknown = ["networkFailed", "readFailed", "unknown_submission", "submission_unknown"].includes(error.code);
    showStatus(unknown ? "unknownSubmission" : errorKey(error), "error");
  } finally {
    state.busy = false;
    render();
    void refreshLog();
  }
});

document.querySelectorAll("[data-language]").forEach((button) => button.addEventListener("click", () => {
  const language = button.dataset.language;
  try { localStorage.setItem(languageKey, language); } catch { /* Storage can be disabled. */ }
  setLanguage(language);
  void refreshLog();
}));

log.addEventListener("toggle", () => {
  if (log.open) void refreshLog();
  else {
    diagnosticsRevision += 1;
    logOutput.textContent = "";
  }
});
window.addEventListener("pagehide", closeCamera);

setLanguage(state.language);
void refreshAccount();
