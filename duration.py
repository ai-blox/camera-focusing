from flask import Flask, Response, request, jsonify
import subprocess
import numpy as np
import cv2
import threading
import re
import time
from datetime import datetime, timedelta

app = Flask(__name__)

current_fps = 0.0
stream_error = None
stream_start_time = None

# Tegrastats cache
tegrastats_data = {"vdd_in": None, "tj": None, "timestamp": 0}
tegrastats_lock = threading.Lock()

# SSH options
SSH_OPTS = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "LogLevel=ERROR",
    "-o", "ConnectTimeout=5",
    "-o", "BatchMode=yes",
]


def tegrastats_updater(ip: str, stop_event: threading.Event):
    """Background thread that streams tegrastats continuously."""
    cmd = ["ssh", *SSH_OPTS, f"root@{ip}", "tegrastats --interval 2000"]
    
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    
    try:
        while not stop_event.is_set():
            line = proc.stdout.readline()
            if not line:
                break

            vdd_match = re.search(r'VDD_IN\s+(\d+)mW', line)
            tj_match = re.search(r'tj@([\d.]+)C', line)

            with tegrastats_lock:
                if vdd_match:
                    tegrastats_data["vdd_in"] = int(vdd_match.group(1))
                if tj_match:
                    tegrastats_data["tj"] = float(tj_match.group(1))
                tegrastats_data["timestamp"] = time.time()
    finally:
        proc.terminate()
        proc.kill()


def fetch_dmesg(ip: str):
    """Fetch last 40 dmesg lines remotely, preventing SSH hang by closing stdin."""
    cmd = ["ssh", *SSH_OPTS, f"root@{ip}", "dmesg -T | tail -n 40"]
    try:
        result = subprocess.run(
            cmd, 
            capture_output=True, 
            text=True, 
            timeout=8, 
            stdin=subprocess.DEVNULL
        )
        
        if result.returncode != 0:
            return [{"ts": "", "text": f"SSH Error: {result.stderr.strip()}", "level": "error"}]

        lines = []
        for line in result.stdout.strip().splitlines():
            level = "info"
            low = line.lower()
            if "error" in low or "fail" in low or "critical" in low:
                level = "error"
            elif "warn" in low:
                level = "warn"
            
            # Parse timestamp: [Wed Apr 15 08:42:24 2026] Message...
            m = re.match(r'^\[(.*?)\]\s+(.*)', line)
            if m:
                raw_ts = m.group(1)
                msg = m.group(2)
                
                # Normalize spaces (e.g. "Apr  5" to "Apr 5") to safely parse
                raw_ts_clean = re.sub(r'\s+', ' ', raw_ts.strip())
                
                try:
                    # Convert to datetime object to safely add 2 hours
                    dt = datetime.strptime(raw_ts_clean, "%a %b %d %H:%M:%S %Y")
                    dt += timedelta(hours=2)
                    ts = dt.strftime("[%d/%m - %H:%M:%S]")
                except ValueError:
                    # Fallback for standard kernel boot times (e.g. [  12.345678])
                    ts = f"[{raw_ts}]"
            else:
                ts = ""
                msg = line

            lines.append({"ts": ts, "text": msg, "level": level})
            
        return lines[-30:]
    except subprocess.TimeoutExpired:
        return [{"ts": "", "text": "Failed to fetch dmesg: Connection timed out.", "level": "error"}]
    except Exception as e:
        return [{"ts": "", "text": f"Failed to fetch dmesg: {str(e)}", "level": "error"}]


def generate_frames(ip: str):
    global current_fps, stream_error, stream_start_time

    WIDTH, HEIGHT = 1920, 1080
    FRAME_SIZE = WIDTH * HEIGHT * 2

    cmd = [
        "ssh", *SSH_OPTS, f"root@{ip}",
        f"v4l2-ctl -d /dev/video1 --set-fmt-video=width={WIDTH},height={HEIGHT},pixelformat=UYVY --stream-mmap --stream-to=-"
    ]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    stream_error = None
    stream_start_time = time.time()

    frame_times = []

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

    try:
        while True:
            t0 = time.time()
            raw = read_with_timeout(proc.stdout, FRAME_SIZE, timeout=3.0)

            if raw is None or len(raw) < FRAME_SIZE:
                stream_error = "no_signal"
                break

            yuv = np.frombuffer(raw, dtype=np.uint8).reshape((HEIGHT, WIDTH, 2))
            bgr = cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR_UYVY)
            
            frame = cv2.resize(bgr, (960, 540))

            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            cv2.putText(frame, ts, (10, 30), cv2.FONT_HERSHEY_DUPLEX, 0.8, (0, 0, 0), 3)
            cv2.putText(frame, ts, (10, 30), cv2.FONT_HERSHEY_DUPLEX, 0.8, (255, 255, 255), 1)

            now = time.time()
            frame_times.append(now)
            frame_times = [ft for ft in frame_times if now - ft < 2.0]
            current_fps = len(frame_times) / 2.0

            ret, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
            if not ret:
                continue

            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + buffer.tobytes() + b'\r\n')

    finally:
        proc.terminate()
        proc.kill()
        current_fps = 0.0
        stream_start_time = None


_tegrastats_stop = None
_tegrastats_thread = None


@app.route("/")
def index():
    return r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Blox S Durability Testing</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Barlow:wght@400;600;700&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg: #0a0e14;
      --surface: #111720;
      --border: #1e2d3d;
      --accent: #00c8ff;
      --accent2: #00ff9d;
      --warn: #ffb347;
      --err: #ff4d4d;
      --text: #ccd6f6;
      --muted: #556070;
      --mono: 'Share Tech Mono', monospace;
      --sans: 'Barlow', sans-serif;
    }

    * { box-sizing: border-box; margin: 0; padding: 0; }

    body {
      background: var(--bg);
      color: var(--text);
      font-family: var(--sans);
      min-height: 100vh;
      padding: 0;
    }

    header {
      background: var(--surface);
      border-bottom: 1px solid var(--border);
      padding: 16px 28px;
      display: flex;
      align-items: center;
      gap: 18px;
    }

    .logo {
      font-family: var(--mono);
      font-size: 22px;
      color: var(--accent);
      letter-spacing: 2px;
      white-space: nowrap;
    }
    .logo span { color: var(--accent2); }

    .header-dot {
      width: 10px; height: 10px;
      border-radius: 50%;
      background: var(--muted);
      flex-shrink: 0;
    }
    .header-dot.live {
      background: var(--accent2);
      box-shadow: 0 0 10px var(--accent2);
      animation: pulse 1.6s ease-in-out infinite;
    }
    @keyframes pulse {
      0%,100% { opacity: 1; } 50% { opacity: 0.4; }
    }

    .controls {
      background: var(--surface);
      border-bottom: 1px solid var(--border);
      padding: 16px 28px;
      display: flex;
      align-items: center;
      gap: 18px;
      flex-wrap: wrap;
    }

    .ctrl-group {
      display: flex;
      align-items: center;
      gap: 10px;
    }

    label {
      font-size: 14px;
      text-transform: uppercase;
      letter-spacing: 1px;
      color: var(--muted);
      font-weight: 600;
    }

    input[type="text"] {
      font-family: var(--mono);
      font-size: 15px;
      background: var(--bg);
      border: 1px solid var(--border);
      color: var(--text);
      padding: 8px 14px;
      border-radius: 4px;
      width: 180px;
      outline: none;
      transition: border-color 0.2s;
    }
    input[type="text"]:focus { border-color: var(--accent); }

    .btn {
      font-family: var(--sans);
      font-size: 14px;
      font-weight: 600;
      letter-spacing: 0.5px;
      padding: 9px 20px;
      border: none;
      border-radius: 4px;
      cursor: pointer;
      transition: opacity 0.15s, transform 0.1s;
    }
    .btn:hover:not(:disabled) { opacity: 0.85; transform: translateY(-1px); }
    .btn:disabled { opacity: 0.35; cursor: not-allowed; }
    .btn-start { background: var(--accent2); color: #000; }
    .btn-stop  { background: var(--warn); color: #000; }

    .stat-pill {
      display: flex;
      align-items: center;
      gap: 8px;
      background: var(--bg);
      border: 1px solid var(--border);
      border-radius: 20px;
      padding: 6px 16px;
      font-family: var(--mono);
      font-size: 15px;
    }
    .stat-pill .pill-label {
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 1px;
    }
    .stat-pill .pill-val { color: var(--accent); font-weight: bold; }
    .stat-pill.temp .pill-val { color: var(--warn); }
    .stat-pill.power .pill-val { color: var(--accent2); }

    .main {
      display: grid;
      /* Increased log column width to 600px */
      grid-template-columns: 1fr 600px;
      gap: 0;
      height: calc(100vh - 130px);
    }

    .video-col {
      display: flex;
      flex-direction: column;
      border-right: 1px solid var(--border);
      overflow: hidden;
      background: #000;
    }

    #feed-container {
      flex: 1;
      display: flex;
      align-items: center;
      justify-content: center;
      position: relative;
      overflow: hidden;
      max-height: 70vh;
    }

    #image {
      width: 100%;
      height: 100%;
      object-fit: contain;
      display: none;
    }

    .offline-overlay {
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 12px;
      color: var(--muted);
    }
    .offline-overlay .big-icon { font-size: 64px; opacity: 0.4; }
    .offline-overlay p { font-size: 16px; }

    .stats-bar {
      background: var(--surface);
      border-top: 1px solid var(--border);
      padding: 16px 24px;
      display: flex;
      gap: 32px;
      align-items: center;
      flex-wrap: wrap;
    }

    .stat-item { display: flex; flex-direction: column; gap: 4px; }
    .stat-item .s-label {
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 1.2px;
      color: var(--muted);
      font-weight: 600;
    }
    .stat-item .s-val {
      font-family: var(--mono);
      font-size: 24px;
      color: var(--accent);
      line-height: 1;
    }
    .stat-item .s-unit {
      font-size: 13px;
      color: var(--muted);
      display: inline;
    }

    .dmesg-col {
      display: flex;
      flex-direction: column;
      overflow: hidden;
      background: var(--bg);
    }

    .panel-header {
      padding: 14px 20px;
      border-bottom: 1px solid var(--border);
      display: flex;
      align-items: center;
      justify-content: space-between;
      background: var(--surface);
    }

    .panel-title {
      font-family: var(--mono);
      font-size: 15px;
      letter-spacing: 2px;
      color: var(--accent);
      text-transform: uppercase;
      font-weight: bold;
    }

    .btn-refresh {
      font-size: 13px;
      background: transparent;
      border: 1px solid var(--border);
      color: var(--muted);
      padding: 5px 12px;
      border-radius: 4px;
      cursor: pointer;
      transition: border-color 0.15s, color 0.15s;
    }
    .btn-refresh:hover { border-color: var(--accent); color: var(--accent); }

    #dmesgLog {
      flex: 1;
      overflow-y: auto;
      padding: 12px 0;
      font-family: var(--mono);
      font-size: 13px;
      line-height: 1.6;
    }

    #dmesgLog::-webkit-scrollbar { width: 6px; }
    #dmesgLog::-webkit-scrollbar-track { background: var(--bg); }
    #dmesgLog::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }

    .log-line {
      padding: 6px 18px;
      border-left: 3px solid transparent;
      transition: background 0.1s;
      word-wrap: break-word;
    }
    
    /* Bumped up opacity and added !important to guarantee striping is visible */
    .log-line:nth-child(even) { background-color: rgba(255, 255, 255, 0.06) !important; }
    .log-line:hover { background-color: rgba(255, 255, 255, 0.09) !important; }
    
    .log-line.info  { color: #8a9bb2; border-left-color: transparent; }
    .log-line.warn  { color: var(--warn); border-left-color: var(--warn); }
    .log-line.error { color: var(--err);  border-left-color: var(--err); }

    .log-line .ts {
      font-weight: 700;
      margin-right: 6px;
    }

    .dmesg-footer {
      border-top: 1px solid var(--border);
      padding: 10px 18px;
      font-size: 12px;
      color: var(--muted);
      font-family: var(--mono);
      background: var(--surface);
    }

    .no-dmesg {
      padding: 30px;
      color: var(--muted);
      font-size: 14px;
      text-align: center;
    }
  </style>
</head>
<body>

<header>
  <div class="logo">Blox S <span>Durability Testing</span></div>
  <div class="header-dot" id="liveDot"></div>
  <span style="font-size:14px; color: var(--muted); font-family: var(--mono); font-weight: bold;" id="headerStatus">OFFLINE</span>
</header>

<div class="controls">
  <div class="ctrl-group">
    <label for="ipInput">Camera IP</label>
    <input type="text" id="ipInput" placeholder="192.168.x.x">
  </div>
  <button class="btn btn-start" id="startBtn" onclick="startStream()">▶ Start</button>
  <button class="btn btn-stop"  id="stopBtn"  onclick="stopStream()" disabled>■ Stop</button>

  <div style="flex:1"></div>

  <div class="stat-pill power">
    <span class="pill-label">Power</span>
    <span class="pill-val" id="vddVal">—</span>
    <span style="color:var(--muted); font-size:13px;">W</span>
  </div>
  <div class="stat-pill temp">
    <span class="pill-label">Temperature</span>
    <span class="pill-val" id="tjVal">—</span>
    <span style="color:var(--muted); font-size:13px;">°C</span>
  </div>
</div>

<div class="main">

  <div class="video-col">
    <div id="feed-container">
      <div class="offline-overlay" id="offlineOverlay">
        <div class="big-icon">📷</div>
        <p>No stream active</p>
      </div>
      <img id="image" draggable="false">
    </div>

    <div class="stats-bar">
      <div class="stat-item">
        <div class="s-label">FPS</div>
        <div class="s-val" id="fpsVal">—</div>
      </div>
      <div class="stat-item">
        <div class="s-label">Uptime</div>
        <div class="s-val" id="uptimeVal">—</div>
      </div>
      <div class="stat-item">
        <div class="s-label">Stream started</div>
        <div class="s-val" id="startedVal" style="font-size:16px; color:var(--muted);">—</div>
      </div>
    </div>
  </div>

  <div class="dmesg-col">
    <div class="panel-header">
      <div class="panel-title">Diagnostic Messages</div>
      <button class="btn-refresh" id="dmesgRefreshBtn" onclick="refreshDmesg()" disabled>↻ Refresh</button>
    </div>
    <div id="dmesgLog">
      <div class="no-dmesg">Start a stream to see diagnostic messages.</div>
    </div>
    <div class="dmesg-footer" id="dmesgFooter">—</div>
  </div>

</div>

<script>
  const img          = document.getElementById("image");
  const startBtn     = document.getElementById("startBtn");
  const stopBtn      = document.getElementById("stopBtn");
  const ipInput      = document.getElementById("ipInput");
  const fpsVal       = document.getElementById("fpsVal");
  const uptimeVal    = document.getElementById("uptimeVal");
  const startedVal   = document.getElementById("startedVal");
  const vddVal       = document.getElementById("vddVal");
  const tjVal        = document.getElementById("tjVal");
  const dmesgLog     = document.getElementById("dmesgLog");
  const dmesgFooter  = document.getElementById("dmesgFooter");
  const liveDot      = document.getElementById("liveDot");
  const headerStatus = document.getElementById("headerStatus");
  const offlineOverlay = document.getElementById("offlineOverlay");
  const dmesgRefreshBtn = document.getElementById("dmesgRefreshBtn");

  let statsInterval  = null;
  let dmesgInterval  = null;
  let errorPollTimer = null;
  let streamStartTs  = null;
  let currentIp      = null;

  function setLive(live) {
    liveDot.className = "header-dot" + (live ? " live" : "");
    headerStatus.textContent = live ? "LIVE" : "OFFLINE";
    headerStatus.style.color = live ? "var(--accent2)" : "";
  }

  function formatUptime(seconds) {
    const d = Math.floor(seconds / 86400);
    const h = Math.floor((seconds % 86400) / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = Math.floor(seconds % 60);
    
    let parts = [];
    if (d > 0) parts.push(`${d} days`);
    if (h > 0) parts.push(`${h} hours`);
    if (m > 0) parts.push(`${m} minutes`);
    parts.push(`${s} seconds`);
    
    return parts.join(" ");
  }

  async function fetchStats() {
    try {
      const [fpsRes, tegraRes] = await Promise.all([
        fetch("/fps"),
        fetch("/tegrastats_data")
      ]);
      const fps   = await fpsRes.json();
      const tegra = await tegraRes.json();

      fpsVal.textContent = fps.fps > 0 ? fps.fps.toFixed(1) : "—";

      if (fps.uptime !== null) {
        uptimeVal.textContent = formatUptime(fps.uptime);
      }

      vddVal.textContent = tegra.vdd_in !== null ? (tegra.vdd_in / 1000).toFixed(2) : "—";
      tjVal.textContent  = tegra.tj     !== null ? tegra.tj.toFixed(1)              : "—";
    } catch(e) {}
  }

  async function refreshDmesg() {
    if (!currentIp) return;
    dmesgFooter.textContent = "Fetching…";
    try {
      const r    = await fetch(`/dmesg?ip=${encodeURIComponent(currentIp)}`);
      const data = await r.json();
      renderDmesg(data.lines);
      dmesgFooter.textContent = `Last updated: ${new Date().toLocaleTimeString()}`;
    } catch(e) {
      dmesgFooter.textContent = "Fetch failed.";
    }
  }

  function renderDmesg(lines) {
    if (!lines || lines.length === 0) {
      dmesgLog.innerHTML = '<div class="no-dmesg">No messages.</div>';
      return;
    }
    
    dmesgLog.innerHTML = lines.map(l => {
      const tsHtml = l.ts ? `<span class="ts">${escHtml(l.ts)}</span>` : "";
      return `<div class="log-line ${l.level}">${tsHtml} <span class="msg">${escHtml(l.text)}</span></div>`;
    }).join("");
    
    dmesgLog.scrollTop = dmesgLog.scrollHeight;
  }

  function escHtml(s) {
    return s.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
  }

  function startStream() {
    const ip = ipInput.value.trim();
    if (!ip) { alert("Enter an IP address first."); return; }
    currentIp = ip;
    streamStartTs = Date.now();
    startedVal.textContent = new Date().toLocaleTimeString();

    img.src = `/video_feed?ip=${encodeURIComponent(ip)}`;
    img.style.display = "block";
    offlineOverlay.style.display = "none";
    setLive(true);

    startBtn.disabled = true;
    stopBtn.disabled  = false;
    ipInput.disabled  = true;
    dmesgRefreshBtn.disabled = false;

    statsInterval = setInterval(fetchStats, 500);
    dmesgInterval = setInterval(refreshDmesg, 15000);
    refreshDmesg();

    errorPollTimer = setInterval(async () => {
      try {
        const r    = await fetch("/stream_error");
        const data = await r.json();
        if (data.error === "no_signal") {
          clearInterval(errorPollTimer); errorPollTimer = null;
          stopStream(true);
        }
      } catch(e) {}
    }, 2000);

    img.onerror = () => stopStream(true);
  }

  function stopStream(isError) {
    img.onerror = null;
    img.src = "";
    img.style.display = "none";
    offlineOverlay.style.display = "flex";
    setLive(false);

    startBtn.disabled = false;
    stopBtn.disabled  = true;
    ipInput.disabled  = false;
    dmesgRefreshBtn.disabled = true;
    fpsVal.textContent    = "—";
    uptimeVal.textContent = "—";
    startedVal.textContent = "—";
    currentIp = null;

    if (statsInterval)  { clearInterval(statsInterval);  statsInterval  = null; }
    if (dmesgInterval)  { clearInterval(dmesgInterval);  dmesgInterval  = null; }
    if (errorPollTimer) { clearInterval(errorPollTimer); errorPollTimer = null; }
  }
</script>
</body>
</html>
"""


@app.route("/video_feed")
def video_feed():
    ip = request.args.get("ip", "")
    global _tegrastats_stop, _tegrastats_thread
    if _tegrastats_stop:
        _tegrastats_stop.set()
    _tegrastats_stop = threading.Event()
    _tegrastats_thread = threading.Thread(
        target=tegrastats_updater, args=(ip, _tegrastats_stop), daemon=True)
    _tegrastats_thread.start()
    return Response(generate_frames(ip), mimetype='multipart/x-mixed-replace; boundary=frame')


@app.route("/fps")
def get_fps():
    uptime = None
    if stream_start_time is not None:
        uptime = time.time() - stream_start_time
    return jsonify({"fps": current_fps, "uptime": uptime})


@app.route("/stream_error")
def get_stream_error():
    return jsonify({"error": stream_error})


@app.route("/tegrastats_data")
def get_tegrastats():
    with tegrastats_lock:
        return jsonify({
            "vdd_in": tegrastats_data["vdd_in"],
            "tj":     tegrastats_data["tj"],
        })


@app.route("/dmesg")
def get_dmesg():
    ip = request.args.get("ip", "")
    lines = fetch_dmesg(ip)
    return jsonify({"lines": lines})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, threaded=True, debug=True)