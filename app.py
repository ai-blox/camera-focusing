from flask import Flask, Response, request, jsonify
import subprocess
import numpy as np
import cv2
import threading
import socket
import platform

app = Flask(__name__)

current_score = 0.0
current_zoom = 4.0
stream_error = None
zoom_offset_x = 0
zoom_offset_y = 0

CAMERA_HOSTNAME = "aibp0046-p3767-0003"

# Shared SSH options to bypass host key mismatch errors
SSH_OPTS = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "LogLevel=ERROR",         # suppress the "Warning: Permanently added..." noise
    "-o", "ConnectTimeout=5",
]


def resolve_hostname_to_ips(hostname: str) -> list[str]:
    """
    Resolve a hostname to all of its IPv4 addresses.
    Returns a (possibly empty) list of unique IP strings.
    """
    try:
        results = socket.getaddrinfo(hostname, None)
        ips = list({r[4][0] for r in results if ":" not in r[4][0]})  # IPv4 only
        return sorted(ips)
    except socket.gaierror:
        return []


def ping_ip(ip: str) -> bool:
    """
    Ping a single IP once. Returns True if the host responds.
    Works on both Windows (ping -n 1 -w 1000) and Linux/macOS (ping -c 1 -W 1).
    """
    system = platform.system().lower()
    if system == "windows":
        cmd = ["ping", "-n", "1", "-w", "1000", ip]
    else:
        cmd = ["ping", "-c", "1", "-W", "1", ip]

    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
        return result.returncode == 0
    except Exception:
        return False


def scan_for_cameras(hostname: str = CAMERA_HOSTNAME) -> list[dict]:
    """
    Resolve the well-known camera hostname and ping every resolved IP in parallel.
    Returns a list of dicts: [{"ip": "...", "hostname": "..."}]
    """
    ips = resolve_hostname_to_ips(hostname)
    if not ips:
        return []

    found = []
    lock = threading.Lock()

    def check(ip):
        if ping_ip(ip):
            with lock:
                found.append({"ip": ip, "hostname": hostname})

    threads = [threading.Thread(target=check, args=(ip,), daemon=True) for ip in ips]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=4)

    return found


def generate_frames(ip: str):
    global current_score, current_zoom, stream_error, zoom_offset_x, zoom_offset_y

    WIDTH, HEIGHT = 1920, 1080
    FRAME_SIZE = WIDTH * HEIGHT * 2

    cmd = [
        "ssh", *SSH_OPTS, f"root@{ip}",
        f"v4l2-ctl -d /dev/video1 --set-fmt-video=width={WIDTH},height={HEIGHT},pixelformat=UYVY --stream-mmap --stream-to=-"
    ]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def read_with_timeout(pipe, size, timeout=3.0):
        result = [None]
        def _read():
            try:
                result[0] = pipe.read(size)
            except Exception:
                result[0] = b""
        t = threading.Thread(target=_read, daemon=True)
        t.start()
        t.join(timeout)
        return result[0]

    stream_error = None

    try:
        while True:
            raw = read_with_timeout(proc.stdout, FRAME_SIZE, timeout=3.0)

            if raw is None:
                stream_error = "no_signal"
                yield (b'--frame\r\n'
                       b'Content-Type: text/event-stream\r\n\r\n'
                       b'ERROR:no_signal\r\n')
                break

            if len(raw) < FRAME_SIZE:
                stream_error = "no_signal"
                break

            Z  = current_zoom
            ox = zoom_offset_x
            oy = zoom_offset_y

            yuv = np.frombuffer(raw, dtype=np.uint8).reshape((HEIGHT, WIDTH, 2))
            bgr = cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR_UYVY)

            half_size = int(min(540, 540 / Z))
            cx = max(half_size, min(WIDTH  - half_size, 960 + ox))
            cy = max(half_size, min(HEIGHT - half_size, 540 + oy))

            zoom_crop = bgr[cy - half_size:cy + half_size,
                            cx - half_size:cx + half_size]

            gray_zoom = cv2.cvtColor(zoom_crop, cv2.COLOR_BGR2GRAY)
            score = cv2.Laplacian(gray_zoom, cv2.CV_64F).var()
            current_score = score

            main_disp = cv2.resize(bgr, (960, 540))
            zoom_disp = cv2.resize(zoom_crop, (540, 540))

            rect_cx   = cx // 2
            rect_cy   = cy // 2
            rect_half = half_size // 2
            cv2.rectangle(main_disp,
                          (rect_cx - rect_half, rect_cy - rect_half),
                          (rect_cx + rect_half, rect_cy + rect_half),
                          (0, 255, 255), 3)

            cv2.putText(zoom_disp, f"{Z:.2f}X ZOOM", (20, 40),
                        cv2.FONT_HERSHEY_DUPLEX, 1.0, (255, 255, 255), 2)

            combined = np.hstack((main_disp, zoom_disp))

            ret, buffer = cv2.imencode('.jpg', combined, [cv2.IMWRITE_JPEG_QUALITY, 80])
            if not ret:
                continue

            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + buffer.tobytes() + b'\r\n')

    finally:
        proc.terminate()
        proc.kill()
        current_score = 0.0


@app.route("/")
def index():
    return r"""
<!DOCTYPE html>
<html>
<head>
  <title>Camera Focus Calibration</title>
  <style>
    body {
      font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
      background: #1e1e1e;
      color: #fff;
      display: flex;
      flex-direction: column;
      align-items: center;
      padding: 30px;
      margin: 0;
    }

    h1 { margin-bottom: 20px; }

    .control-panel {
      background: #2d2d2d;
      padding: 20px;
      border-radius: 8px;
      box-shadow: 0 4px 15px rgba(0,0,0,0.5);
      margin-bottom: 20px;
      display: flex;
      gap: 15px;
      align-items: center;
      flex-wrap: wrap;
      width: 90%;
      max-width: 1200px;
      box-sizing: border-box;
    }

    .group {
      display: flex;
      align-items: center;
      gap: 10px;
      border-right: 1px solid #444;
      padding-right: 15px;
    }
    .group:last-child { border-right: none; }

    input[type="text"] {
      padding: 10px;
      font-size: 16px;
      border-radius: 4px;
      border: 1px solid #555;
      background: #444;
      color: white;
      width: 160px;
    }

    input[type="range"] {
      width: 150px;
      cursor: pointer;
    }

    button {
      padding: 10px 20px;
      font-size: 16px;
      border: none;
      border-radius: 4px;
      cursor: pointer;
      font-weight: bold;
      transition: opacity 0.2s;
    }
    button:hover { opacity: 0.8; }
    button:disabled { opacity: 0.4; cursor: not-allowed; }

    #startBtn { background-color: #28a745; color: white; }
    #stopBtn  { background-color: #ffc107; color: black; }
    #powerBtn { background-color: #dc3545; color: white; margin-left: auto; }

    #feed-container {
      position: relative;
      width: 90%;
      max-width: 1200px;
      background: #000;
      border: 2px solid #444;
      border-radius: 8px 8px 0 0;
      overflow: hidden;
      min-height: 400px;
      display: flex;
      justify-content: center;
      align-items: center;
      box-sizing: border-box;
      transition: border-color 0.3s ease;
    }

    #feed-container.offline-state  { border-color: #444; }
    #feed-container.error-state    { border-color: #dc3545; }
    #feed-container.poweroff-state { border-color: #6c757d; }

    #image {
      width: 100%;
      object-fit: contain;
      display: none;
      user-select: none;
      -webkit-user-drag: none;
    }

    /* Shared banner style for all three states */
    .info-banner {
      display: none;
      flex-direction: column;
      align-items: center;
      gap: 8px;
    }
    .info-banner .banner-icon { font-size: 48px; }
    .info-banner .banner-msg  { font-size: 20px; font-weight: bold; }
    .info-banner .banner-sub  { color: #aaa; font-size: 14px; }

    /* ── Network scan UI ─────────────────────────────────────────────────── */
    #scanStatus {
      margin-top: 14px;
      font-size: 13px;
      color: #aaa;
      display: flex;
      align-items: center;
      gap: 8px;
      min-height: 22px;
    }

    .spinner {
      display: inline-block;
      width: 14px; height: 14px;
      border: 2px solid #666;
      border-top-color: #fff;
      border-radius: 50%;
      animation: spin 0.7s linear infinite;
      flex-shrink: 0;
    }
    @keyframes spin { to { transform: rotate(360deg); } }

    #cameraList {
      margin-top: 10px;
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      justify-content: center;
    }

    .camera-chip {
      background: #3a3a3a;
      border: 1px solid #555;
      border-radius: 20px;
      padding: 6px 14px;
      font-size: 14px;
      cursor: pointer;
      transition: background 0.15s, border-color 0.15s;
      display: flex;
      align-items: center;
      gap: 6px;
    }
    .camera-chip:hover {
      background: #28a745;
      border-color: #28a745;
      color: #fff;
    }
    .camera-chip .chip-ip       { font-weight: bold; }
    .camera-chip .chip-hostname { color: #aaa; font-size: 12px; }
    .camera-chip:hover .chip-hostname { color: #d0ffd0; }

    /* ── Drag / reset overlay ────────────────────────────────────────────── */
    #drag-overlay {
      display: none;
      position: absolute;
      inset: 0;
      cursor: grab;
      z-index: 10;
    }
    #drag-overlay.dragging { cursor: grabbing; }

    #resetOffsetBtn {
      display: none;
      position: absolute;
      top: 8px;
      right: 8px;
      z-index: 20;
      padding: 4px 10px;
      font-size: 13px;
      background: rgba(0,0,0,0.6);
      color: #fff;
      border: 1px solid #888;
      border-radius: 4px;
      cursor: pointer;
    }
    #resetOffsetBtn:hover { background: rgba(80,80,80,0.8); }

    .score-container {
      width: 90%;
      max-width: 1200px;
      background: #2d2d2d;
      padding: 20px;
      border: 2px solid #444;
      border-top: none;
      border-radius: 0 0 8px 8px;
      box-sizing: border-box;
      display: none;
      justify-content: center;
      align-items: center;
    }

    .score-text   { font-size: 32px; font-weight: bold; text-shadow: 1px 1px 2px rgba(0,0,0,0.5); }
    .score-label  { color: #ffffff; }
    .score-number { transition: color 0.1s ease-out; }
  </style>
</head>
<body>
  <h1>Camera Focus Calibration</h1>

  <div class="control-panel">
    <div class="group">
      <label for="ipInput">IP:</label>
      <input type="text" id="ipInput" value="" placeholder="192.168.x.x">
    </div>

    <div class="group">
      <label for="zoomSlider">Zoom:</label>
      <input type="range" id="zoomSlider" min="1.0" max="12.0" step="0.25" value="4.0" oninput="updateZoom(this.value)">
      <span id="zoomLabel" style="min-width: 45px; font-weight: bold;">4.00x</span>
    </div>

    <div class="group" style="border-right: none;">
      <button id="startBtn" onclick="startStream()">&#9654; Start Stream</button>
      <button id="stopBtn"  onclick="stopStream()" disabled>&#9646;&#9646; Stop Stream</button>
    </div>

    <button id="powerBtn" onclick="powerOffCamera()">&#9211; Power OFF Camera</button>
  </div>

  <div id="feed-container" class="offline-state">

    <!-- Offline banner (shown on load and after stop) -->
    <div id="offlineBanner" class="info-banner" style="display:flex;">
      <span class="banner-icon">&#127909;</span>
      <span class="banner-msg" style="color:#ffffff;">Stream Offline</span>
      <span class="banner-sub">Enter an IP address and click Start, or select a discovered camera below.</span>
      <div id="scanStatus"></div>
      <div id="cameraList"></div>
    </div>

    <!-- No-signal banner -->
    <div id="errorBanner" class="info-banner">
      <span class="banner-icon">&#128225;</span>
      <span class="banner-msg" style="color:#ff6b6b;">No Signal</span>
      <span class="banner-sub" id="errorDetail">Could not connect to camera. Check the IP address and try again.</span>
    </div>

    <!-- Powered-off banner -->
    <div id="poweredOffBanner" class="info-banner">
      <span class="banner-icon">&#9211;</span>
      <span class="banner-msg" style="color:#aaa;">Powered OFF</span>
      <span class="banner-sub" id="poweredOffDetail">The remote device has been shut down.</span>
    </div>

    <img id="image" draggable="false" />
    <div id="drag-overlay"></div>
    <button id="resetOffsetBtn" onclick="resetOffset()">&#8859; Re-centre</button>
  </div>

  <div class="score-container" id="scoreContainer">
    <div class="score-text">
      <span class="score-label">Sharpness: </span><span class="score-number" id="scoreValue"></span>
    </div>
  </div>

  <script>
    const img              = document.getElementById("image");
    const startBtn         = document.getElementById("startBtn");
    const stopBtn          = document.getElementById("stopBtn");
    const ipInput          = document.getElementById("ipInput");
    const scoreContainer   = document.getElementById("scoreContainer");
    const scoreValue       = document.getElementById("scoreValue");
    const zoomLabel        = document.getElementById("zoomLabel");
    const feedContainer    = document.getElementById("feed-container");
    const offlineBanner    = document.getElementById("offlineBanner");
    const errorBanner      = document.getElementById("errorBanner");
    const errorDetail      = document.getElementById("errorDetail");
    const poweredOffBanner = document.getElementById("poweredOffBanner");
    const poweredOffDetail = document.getElementById("poweredOffDetail");
    const dragOverlay      = document.getElementById("drag-overlay");
    const resetOffsetBtn   = document.getElementById("resetOffsetBtn");
    const scanStatus       = document.getElementById("scanStatus");
    const cameraList       = document.getElementById("cameraList");

    let scoreInterval   = null;
    let errorPollTimer  = null;
    let scanInterval    = null;
    let scanInProgress  = false;

    const MAX_EXPECTED_SCORE = 4000.0;
    const SCAN_INTERVAL_MS   = 10_000;   // re-scan every 10 s while offline

    // ── Banner helpers ────────────────────────────────────────────────────────
    function hideAllBanners() {
      offlineBanner.style.display    = "none";
      errorBanner.style.display      = "none";
      poweredOffBanner.style.display = "none";
      feedContainer.classList.remove("offline-state", "error-state", "poweroff-state");
    }

    function showOffline() {
      hideAllBanners();
      offlineBanner.style.display = "flex";
      feedContainer.classList.add("offline-state");
      startNetworkScan();   // kick off a scan right away
    }

    function showError(detail) {
      stopNetworkScan();
      hideAllBanners();
      errorBanner.style.display = "flex";
      errorDetail.textContent   = detail || "Could not connect to camera. Check the IP address and try again.";
      feedContainer.classList.add("error-state");
      img.style.display         = "none";
      dragOverlay.style.display = "none";
      scoreContainer.style.display = "none";
      startBtn.disabled = false;
      stopBtn.disabled  = true;
      ipInput.disabled  = false;
    }

    function showPoweredOff(ip) {
      stopNetworkScan();
      hideAllBanners();
      poweredOffBanner.style.display = "flex";
      poweredOffDetail.textContent   = `${ip} has been shut down. It is safe to swap hardware.`;
      feedContainer.classList.add("poweroff-state");
      img.style.display              = "none";
      dragOverlay.style.display      = "none";
      scoreContainer.style.display   = "none";
      scoreValue.textContent         = "";
      startBtn.disabled = false;
      stopBtn.disabled  = true;
      ipInput.disabled  = false;
    }

    // ── Network scan ──────────────────────────────────────────────────────────
    function startNetworkScan() {
      runScan();
      if (!scanInterval) {
        scanInterval = setInterval(runScan, SCAN_INTERVAL_MS);
      }
    }

    function stopNetworkScan() {
      if (scanInterval) { clearInterval(scanInterval); scanInterval = null; }
      scanInProgress = false;
      scanStatus.innerHTML = "";
      cameraList.innerHTML = "";
    }

    async function runScan() {
      if (scanInProgress) return;
      scanInProgress = true;
      scanStatus.innerHTML = '<span class="spinner"></span> Scanning network for cameras&hellip;';
      cameraList.innerHTML = "";

      try {
        const r    = await fetch("/scan_cameras");
        const data = await r.json();

        if (data.cameras && data.cameras.length > 0) {
          scanStatus.innerHTML = `&#10003; Found ${data.cameras.length} camera${data.cameras.length > 1 ? "s" : ""}`;
          cameraList.innerHTML = "";
          data.cameras.forEach(cam => {
            const chip = document.createElement("div");
            chip.className = "camera-chip";
            chip.title     = `Click to use ${cam.ip}`;
            chip.innerHTML = `<span class="chip-ip">&#128247; ${cam.ip}</span>
                              <span class="chip-hostname">${cam.hostname}</span>`;
            chip.addEventListener("click", () => {
              ipInput.value = cam.ip;
              // Briefly highlight the IP input so the user sees it was filled
              ipInput.style.borderColor = "#28a745";
              setTimeout(() => { ipInput.style.borderColor = ""; }, 1200);
            });
            cameraList.appendChild(chip);
          });
        } else {
          scanStatus.innerHTML = "&#128270; No cameras found &mdash; retrying in 10s";
          cameraList.innerHTML = "";
        }
      } catch(e) {
        scanStatus.innerHTML = "&#9888; Scan failed &mdash; retrying in 10s";
      } finally {
        scanInProgress = false;
      }
    }

    // ── Drag logic ────────────────────────────────────────────────────────────
    let offsetX = 0, offsetY = 0;
    let dragStartMouseX = 0, dragStartMouseY = 0;
    let dragStartOffsetX = 0, dragStartOffsetY = 0;
    let isDragging = false;
    const MAIN_VIEW_FRACTION = 960 / 1500;

    function getMainViewScale() {
      if (!img.naturalWidth) return 2;
      return (img.naturalWidth / img.getBoundingClientRect().width) * 2;
    }

    dragOverlay.addEventListener("mousedown", onDragStart);
    window.addEventListener("mousemove", onDragMove);
    window.addEventListener("mouseup",   onDragEnd);
    dragOverlay.addEventListener("touchstart", e => onDragStart(e.touches[0]), { passive: true });
    window.addEventListener("touchmove",  e => onDragMove(e.touches[0]),  { passive: true });
    window.addEventListener("touchend",   onDragEnd);

    function onDragStart(e) {
      const rect = dragOverlay.getBoundingClientRect();
      if ((e.clientX - rect.left) / rect.width > MAIN_VIEW_FRACTION) return;
      isDragging = true;
      dragOverlay.classList.add("dragging");
      dragStartMouseX  = e.clientX;
      dragStartMouseY  = e.clientY;
      dragStartOffsetX = offsetX;
      dragStartOffsetY = offsetY;
    }

    function onDragMove(e) {
      if (!isDragging) return;
      const scale = getMainViewScale();
      offsetX = Math.round(dragStartOffsetX + (e.clientX - dragStartMouseX) * scale);
      offsetY = Math.round(dragStartOffsetY + (e.clientY - dragStartMouseY) * scale);
      resetOffsetBtn.style.display = (offsetX !== 0 || offsetY !== 0) ? "block" : "none";
      sendOffset();
    }

    function onDragEnd() {
      if (!isDragging) return;
      isDragging = false;
      dragOverlay.classList.remove("dragging");
    }

    function resetOffset() {
      offsetX = 0; offsetY = 0;
      resetOffsetBtn.style.display = "none";
      sendOffset();
    }

    async function sendOffset() {
      try {
        await fetch("/set_offset", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ox: offsetX, oy: offsetY })
        });
      } catch(e) { console.error("Failed to send offset"); }
    }

    // ── Stream control ────────────────────────────────────────────────────────
    function startStream() {
      const ip = ipInput.value.trim();
      if (!ip) { alert("Please enter an IP address."); return; }

      stopNetworkScan();          // no need to scan while a stream is active
      hideAllBanners();
      img.src = `/video_feed?ip=${encodeURIComponent(ip)}`;
      img.style.display            = "block";
      scoreContainer.style.display = "flex";
      dragOverlay.style.display    = "block";

      startBtn.disabled = true;
      stopBtn.disabled  = false;
      ipInput.disabled  = true;

      scoreInterval = setInterval(fetchScore, 150);
      errorPollTimer = setInterval(async () => {
        try {
          const r    = await fetch("/stream_error");
          const data = await r.json();
          if (data.error === "no_signal") {
            clearInterval(scoreInterval);
            clearInterval(errorPollTimer);
            scoreInterval  = null;
            errorPollTimer = null;
            showError(`No frames received from ${ip}. Check the IP and camera connection.`);
          }
        } catch(e) {}
      }, 2000);

      img.onerror = () => {
        clearInterval(scoreInterval);
        clearInterval(errorPollTimer);
        scoreInterval  = null;
        errorPollTimer = null;
        showError(`Could not reach ${ip}. Check the IP address and network connection.`);
      };
    }

    function stopStream() {
      img.onerror = null;
      img.src = "";
      img.style.display            = "none";
      dragOverlay.style.display    = "none";
      scoreContainer.style.display = "none";
      scoreValue.textContent       = "";
      scoreValue.style.color       = "#ffffff";
      startBtn.disabled = false;
      stopBtn.disabled  = true;
      ipInput.disabled  = false;
      if (scoreInterval)  { clearInterval(scoreInterval);  scoreInterval  = null; }
      if (errorPollTimer) { clearInterval(errorPollTimer); errorPollTimer = null; }
      showOffline();
    }

    async function updateZoom(value) {
      zoomLabel.textContent = parseFloat(value).toFixed(2) + "x";
      try {
        await fetch("/set_zoom", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ zoom: value })
        });
      } catch(e) { console.error("Failed to update zoom on server."); }
    }

    async function fetchScore() {
      try {
        const response = await fetch("/score");
        const data     = await response.json();
        const score    = data.score;
        if (score === 0) {
          scoreValue.textContent = "";
          scoreValue.style.color = "#ffffff";
          return;
        }
        scoreValue.textContent = score.toFixed(1);
        const ratio = Math.min(1, Math.max(0, score / MAX_EXPECTED_SCORE));
        scoreValue.style.color = `hsl(${Math.floor(ratio * 120)}, 100%, 50%)`;
      } catch(e) { console.error("Failed to fetch score"); }
    }

    async function powerOffCamera() {
      if (!confirm("Are you sure you want to power down the remote device? This will run 'shutdown -h now'.")) return;

      const ip = ipInput.value.trim();
      img.onerror = null;
      img.src = "";
      img.style.display            = "none";
      dragOverlay.style.display    = "none";
      scoreContainer.style.display = "none";
      scoreValue.textContent       = "";
      scoreValue.style.color       = "#ffffff";
      startBtn.disabled = false;
      stopBtn.disabled  = true;
      ipInput.disabled  = false;

      if (scoreInterval)  { clearInterval(scoreInterval);  scoreInterval  = null; }
      if (errorPollTimer) { clearInterval(errorPollTimer); errorPollTimer = null; }

      showPoweredOff(ip);

      try {
        const res  = await fetch("/power_off", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ip })
        });
        const data = await res.json();
        poweredOffDetail.textContent = data.message;
      } catch(err) {
        poweredOffDetail.textContent = "Failed to send shutdown command.";
      }
    }

    // ── Init: show offline banner (which triggers the first scan) ─────────────
    showOffline();
  </script>
</body>
</html>
"""


@app.route("/video_feed")
def video_feed():
    ip = request.args.get("ip", "192.168.7.216")
    return Response(generate_frames(ip), mimetype='multipart/x-mixed-replace; boundary=frame')


@app.route("/score")
def get_score():
    return jsonify({"score": current_score})


@app.route("/stream_error")
def get_stream_error():
    return jsonify({"error": stream_error})


@app.route("/scan_cameras")
def scan_cameras():
    """
    Resolve the well-known camera hostname and ping all resolved IPs.
    Runs the work on a background thread so the main thread never blocks,
    but we still wait for results before replying (the scan is fast).
    """
    cameras = scan_for_cameras(CAMERA_HOSTNAME)
    return jsonify({"cameras": cameras})


@app.route("/set_zoom", methods=["POST"])
def set_zoom():
    global current_zoom
    data = request.json
    if data and "zoom" in data:
        try:
            current_zoom = max(1.0, min(12.0, float(data["zoom"])))
            return jsonify({"status": "success", "zoom": current_zoom})
        except ValueError:
            pass
    return jsonify({"status": "error", "message": "Invalid zoom value"}), 400


@app.route("/set_offset", methods=["POST"])
def set_offset():
    global zoom_offset_x, zoom_offset_y
    data = request.json
    if data and "ox" in data and "oy" in data:
        try:
            zoom_offset_x = int(data["ox"])
            zoom_offset_y = int(data["oy"])
            return jsonify({"status": "success", "ox": zoom_offset_x, "oy": zoom_offset_y})
        except (ValueError, TypeError):
            pass
    return jsonify({"status": "error", "message": "Invalid offset"}), 400


@app.route("/power_off", methods=["POST"])
def power_off():
    ip = request.json.get("ip")
    if not ip:
        return jsonify({"status": "error", "message": "No IP provided"}), 400
    cmd = ["ssh", *SSH_OPTS, f"root@{ip}", "shutdown -h now"]
    try:
        subprocess.run(cmd, timeout=10)
        return jsonify({"status": "success", "message": f"Shutdown command sent to {ip}. Device is powering off."})
    except subprocess.TimeoutExpired:
        return jsonify({"status": "success", "message": f"Shutdown command sent to {ip} (connection dropped as expected)."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5002, threaded=True, debug=True)