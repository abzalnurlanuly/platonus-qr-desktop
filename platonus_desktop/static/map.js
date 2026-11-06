"use strict";

const tileSize = 256;
const maxMercatorLatitude = 85.05112878;

function wrapCoordinate(value, range) {
  return ((value % range) + range) % range;
}

function projectLocation(latitude, longitude, size) {
  const radians = Math.max(-maxMercatorLatitude, Math.min(maxMercatorLatitude, latitude)) * Math.PI / 180;
  return {
    x: wrapCoordinate((longitude + 180) / 360 * size, size),
    y: (0.5 - Math.asinh(Math.tan(radians)) / (2 * Math.PI)) * size
  };
}

function unprojectLocation(x, y, size) {
  return {
    latitude: Math.atan(Math.sinh(Math.PI * (1 - 2 * y / size))) * 180 / Math.PI,
    longitude: wrapCoordinate(x, size) / size * 360 - 180
  };
}

class AttendanceMap {
  constructor(onSelect) {
    this.viewport = document.getElementById("map");
    this.layer = document.getElementById("map-tiles");
    this.pin = document.getElementById("map-pin");
    this.unavailable = document.getElementById("map-error");
    this.zoomIn = document.getElementById("zoom-in");
    this.zoomOut = document.getElementById("zoom-out");
    this.onSelect = onSelect;
    this.tiles = new Map();
    this.zoom = 2;
    this.center = projectLocation(21, 10, this.size);
    this.selection = null;
    this.active = false;
    this.drag = null;

    this.zoomIn.addEventListener("click", () => this.changeZoom(1));
    this.zoomOut.addEventListener("click", () => this.changeZoom(-1));
    this.viewport.addEventListener("keydown", (event) => this.keyDown(event));
    this.viewport.addEventListener("pointerdown", (event) => this.startDrag(event));
    this.viewport.addEventListener("pointermove", (event) => this.moveDrag(event));
    this.viewport.addEventListener("pointerup", (event) => this.endDrag(event));
    this.viewport.addEventListener("pointercancel", () => this.clearDrag());
    new ResizeObserver(() => this.render()).observe(this.viewport);
  }

  get size() {
    return tileSize * 2 ** this.zoom;
  }

  open() {
    this.active = true;
    this.render();
  }

  close() {
    this.active = false;
    this.clearDrag();
    this.layer.replaceChildren();
    this.tiles.clear();
    this.unavailable.hidden = true;
  }

  select(location) {
    this.selection = location;
    this.renderPin();
  }

  changeZoom(amount) {
    if (!this.active) return;
    const next = Math.max(2, Math.min(19, this.zoom + amount));
    if (next === this.zoom) return;
    const anchor = this.selection && !this.pin.hidden
      ? this.selection : unprojectLocation(this.center.x, this.center.y, this.size);
    this.zoom = next;
    this.center = projectLocation(anchor.latitude, anchor.longitude, this.size);
    this.render();
  }

  shift(x, y) {
    this.center.x = wrapCoordinate(this.center.x + x, this.size);
    this.center.y = Math.max(0, Math.min(this.size - 1, this.center.y + y));
    this.render();
  }

  keyDown(event) {
    if (!this.active) return;
    switch (event.key) {
      case "ArrowLeft": this.shift(-110, 0); break;
      case "ArrowRight": this.shift(110, 0); break;
      case "ArrowUp": this.shift(0, -110); break;
      case "ArrowDown": this.shift(0, 110); break;
      case "+":
      case "=": this.changeZoom(1); break;
      case "-": this.changeZoom(-1); break;
      case "Enter": this.pick(this.viewport.clientWidth / 2, this.viewport.clientHeight / 2); break;
      default: return;
    }
    event.preventDefault();
  }

  startDrag(event) {
    if (!this.active || event.button !== 0 || this.drag) return;
    this.drag = { id: event.pointerId, x: event.clientX, y: event.clientY, moved: false };
    this.viewport.setPointerCapture(event.pointerId);
  }

  moveDrag(event) {
    if (!this.drag || this.drag.id !== event.pointerId) return;
    const x = event.clientX - this.drag.x;
    const y = event.clientY - this.drag.y;
    if (!this.drag.moved && Math.hypot(x, y) < 5) return;
    this.drag.moved = true;
    this.viewport.dataset.dragging = "true";
    this.layer.style.transform = "translate(" + x + "px, " + y + "px)";
    this.pin.style.marginLeft = x + "px";
    this.pin.style.marginTop = y + "px";
  }

  endDrag(event) {
    if (!this.drag || this.drag.id !== event.pointerId) return;
    const drag = this.drag;
    this.clearDrag();
    if (drag.moved) {
      this.shift(drag.x - event.clientX, drag.y - event.clientY);
    } else {
      const rect = this.viewport.getBoundingClientRect();
      this.pick(event.clientX - rect.left, event.clientY - rect.top);
    }
  }

  clearDrag() {
    if (this.drag && this.viewport.hasPointerCapture(this.drag.id)) {
      this.viewport.releasePointerCapture(this.drag.id);
    }
    this.drag = null;
    delete this.viewport.dataset.dragging;
    this.layer.style.transform = "";
    this.pin.style.marginLeft = "";
    this.pin.style.marginTop = "";
  }

  pick(viewX, viewY) {
    const x = this.center.x - this.viewport.clientWidth / 2 + viewX;
    const y = this.center.y - this.viewport.clientHeight / 2 + viewY;
    if (y < 0 || y >= this.size) return;
    const key = this.zoom + ":" + Math.floor(x / tileSize) + ":" + Math.floor(y / tileSize);
    if (this.tiles.get(key)?.dataset.state !== "loaded") return;
    this.select(unprojectLocation(x, y, this.size));
    this.onSelect(this.selection);
  }

  render() {
    if (!this.active || !this.viewport.clientWidth || !this.viewport.clientHeight) return;
    const width = this.viewport.clientWidth;
    const height = this.viewport.clientHeight;
    const left = this.center.x - width / 2;
    const top = this.center.y - height / 2;
    const count = 2 ** this.zoom;
    const visible = new Set();

    for (let y = Math.max(0, Math.floor(top / tileSize)); y < Math.min(count, Math.ceil((top + height) / tileSize)); y += 1) {
      for (let x = Math.floor(left / tileSize); x < Math.ceil((left + width) / tileSize); x += 1) {
        const key = this.zoom + ":" + x + ":" + y;
        visible.add(key);
        let image = this.tiles.get(key);
        if (!image) {
          image = new Image();
          image.alt = "";
          image.draggable = false;
          image.referrerPolicy = "origin";
          image.dataset.state = "pending";
          image.addEventListener("load", () => {
            image.dataset.state = "loaded";
            this.updateAvailability();
          });
          image.addEventListener("error", () => {
            image.dataset.state = "failed";
            this.updateAvailability();
          });
          this.tiles.set(key, image);
          this.layer.append(image);
          image.src = "https://tile.openstreetmap.org/" + this.zoom + "/" + wrapCoordinate(x, count) + "/" + y + ".png";
        }
        image.style.left = Math.round(x * tileSize - left) + "px";
        image.style.top = Math.round(y * tileSize - top) + "px";
      }
    }

    for (const [key, image] of this.tiles) {
      if (visible.has(key)) continue;
      image.remove();
      this.tiles.delete(key);
    }

    this.zoomIn.disabled = this.zoom === 19;
    this.zoomOut.disabled = this.zoom === 2;
    this.updateAvailability();
    this.renderPin();
  }

  updateAvailability() {
    this.unavailable.hidden = !this.active || !this.tiles.size ||
      Array.from(this.tiles.values()).some((tile) => tile.dataset.state !== "failed");
  }

  renderPin() {
    if (!this.active || !this.selection) {
      this.pin.hidden = true;
      return;
    }
    const point = projectLocation(this.selection.latitude, this.selection.longitude, this.size);
    const offset = wrapCoordinate(point.x - this.center.x + this.size / 2, this.size) - this.size / 2;
    const x = this.viewport.clientWidth / 2 + offset;
    const y = this.viewport.clientHeight / 2 + point.y - this.center.y;
    this.pin.hidden = x < 0 || x > this.viewport.clientWidth || y < 0 || y > this.viewport.clientHeight;
    this.pin.style.left = x + "px";
    this.pin.style.top = y + "px";
  }
}
