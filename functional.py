from flask import Flask, Response, request, jsonify
import subprocess
import numpy as np
import cv2
import threading
import re
import time
import socket
import platform
import queue

app = Flask(__name__)

# --- Global States ---
current_fps = 0.0
stream_start_time = None
remote_pids = {"led": None, "gpio": None}
discovered_devices = set()

SSH_OPTS = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "LogLevel=ERROR",
    "-o", "ConnectTimeout=3",
    "-o", "BatchMode=yes",
]

# ==========================================
# EXACT USER PROVIDED LED SCRIPT (BREATHING)
# ==========================================
ORIGINAL_LED_SCRIPT = r"""#!/bin/sh
lp5815_path="/sys/bus/i2c/drivers/lp5815/1-002d"
chip_setup_path="$lp5815_path/lp5815_chip_setup"
chip_enable="$chip_setup_path/device_enable"

autonomousSetup() {
	local led_path=$1
	local autonomous_dir="$led_path/autonomous"
	local Engine0_path="$autonomous_dir/Engine0"
	local order_path="$Engine0_path/Engine0_Order0"
	local pattern_path="$order_path/PATTERN0"

	echo 1 > "$Engine0_path/engine_enable"
	echo 1 > "$order_path/order_enable"
	echo 1 > "$pattern_path/pattern_enable"
	echo 3 > "$Engine0_path/engine_repeat_time"
	echo 15 > "$pattern_path/repeat_time"
	echo 9:9 > "$pattern_path/pause_time"
	echo 11:11 > "$pattern_path/sloper_time1"
	echo 11:11 > "$pattern_path/sloper_time2"
	echo 170 > "$pattern_path/pwm0"
	echo 100 > "$pattern_path/pwm1"
	echo 20 > "$pattern_path/pwm2"
	echo 100 > "$pattern_path/pwm3"
	echo 170 > "$pattern_path/pwm4"
}

led=$1
stat=$2
led_path="$lp5815_path/$led"

echo 1 > "$chip_enable"
echo 1 > "$chip_setup_path/charging_mode"

if [ "$stat" = "start" ]; then
	echo 1 > "$led_path/led_enable"
	echo autonomous > "$led_path/led_mode"
	echo 150 > "$led_path/dot_current"
	autonomousSetup "$led_path"
	echo start > "$chip_setup_path/device_command"
elif [ "$stat" = "stop" ]; then
	echo stop > "$chip_setup_path/device_command"
	echo 0 > "$led_path/led_enable"
fi
"""

# ==========================================
# NEW RAINBOW SCRIPT
# ==========================================
RAINBOW_LED_SCRIPT = r"""#!/bin/bash
lp5815_path="/sys/bus/i2c/drivers/lp5815/1-002d"
chip_setup_path="$lp5815_path/lp5815_chip_setup"

# 1. Wake up the chip identically to the working script
echo 1 > "$chip_setup_path/device_enable"
echo 1 > "$chip_setup_path/charging_mode"

# 2. Set to Direct Mode
for LED in LED0 LED1 LED2; do
    echo 1 > "$lp5815_path/$LED/led_enable"
    echo direct > "$lp5815_path/$LED/led_mode"
done

# 3. Start the chip state machine
echo start > "$chip_setup_path/device_command"

# 4. Helper function to set R, G, B current
update_colors() {
    echo $1 > "$lp5815_path/LED0/dot_current"
    echo $2 > "$lp5815_path/LED1/dot_current"
    echo $3 > "$lp5815_path/LED2/dot_current"
    sleep 0.05
}

# 5. Rainbow Phase Loop (Capped at 150 to match your brightness)
while true; do
    for ((i=0; i<=150; i+=10)); do update_colors 150 $i 0; done
    for ((i=150; i>=0; i-=10)); do update_colors $i 150 0; done
    for ((i=0; i<=150; i+=10)); do update_colors 0 150 $i; done
    for ((i=150; i>=0; i-=10)); do update_colors 0 $i 150; done
    for ((i=0; i<=150; i+=10)); do update_colors $i 0 150; done
    for ((i=150; i>=0; i-=10)); do update_colors 150 0 $i; done
done
"""

# ==========================================
# NETWORK SCANNER
# ==========================================

def sweep_and_scan():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        base_ip = ".".join(local_ip.split(".")[:-1])
        
        def ping_ip(ip):
            cmd = ["ping", "-n", "1", "-w", "500", ip] if platform.system().lower() == "windows" else ["ping", "-c", "1", "-W", "1", ip]
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            
        for i in range(1, 255):
            threading.Thread(target=ping_ip, args=(f"{base_ip}.{i}",), daemon=True).start()
            
        time.sleep(2)
        while True:
            try:
                arp_out = subprocess.check_output(["arp", "-a"], text=True)
                ips = re.findall(r'\b(?:\d{1,3}\.){3}\d{1,3}\b', arp_out)
                for ip in set(ips):
                    if ip.startswith("127.") or ip.endswith(".255") or ip == local_ip: continue
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock.settimeout(0.2)
                    if sock.connect_ex((ip, 22)) == 0:
                        discovered_devices.add(ip)
                    sock.close()
            except: pass
            time.sleep(10)
    except: pass

threading.Thread(target=sweep_and_scan, daemon=True).start()

# ==========================================
# CORE LOGIC
# ==========================================

def fetch_dmesg(ip: str):
    cmd = ["ssh", *SSH_OPTS, f"root@{ip}", "dmesg -T | tail -n 30"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        if result.returncode != 0: return {"all_lines": []}
        all_lines = []
        for line in result.stdout.strip().splitlines():
            level = "error" if any(x in line.lower() for x in ["error", "fail"]) else "warn" if "warn" in line.lower() else "info"
            all_lines.append({"text": line, "level": level})
        return {"all_lines": all_lines}
    except: return {"all_lines": []}

def generate_frames(ip: str):
    global current_fps, stream_start_time
    WIDTH, HEIGHT = 1920, 1080
    FRAME_SIZE = WIDTH * HEIGHT * 2
    cmd = ["ssh", *SSH_OPTS, f"root@{ip}", f"v4l2-ctl -d /dev/video1 --set-fmt-video=width={WIDTH},height={HEIGHT},pixelformat=UYVY --stream-mmap --stream-to=-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    stream_start_time = time.time()
    frame_times = []
    frame_queue = queue.Queue(maxsize=1)

    def pipe_reader():
        try:
            while proc.poll() is None:
                raw = proc.stdout.read(FRAME_SIZE)
                if not raw: break
                while proc.poll() is None:
                    try: frame_queue.put(raw, timeout=0.1); break
                    except queue.Full: pass
        except: pass

    threading.Thread(target=pipe_reader, daemon=True).start()

    try:
        while True:
            try: raw = frame_queue.get(timeout=5.0)
            except queue.Empty: break
            yuv = np.frombuffer(raw, dtype=np.uint8).reshape((HEIGHT, WIDTH, 2))
            bgr = cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR_UYVY)
            frame = cv2.resize(bgr, (960, 540))
            now = time.time()
            frame_times.append(now)
            frame_times = [ft for ft in frame_times if now - ft < 2.0]
            current_fps = len(frame_times) / 2.0
            ret, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
            yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + buffer.tobytes() + b'\r\n')
    finally:
        proc.terminate(); proc.kill()

def run_remote_script(ip, script_content):
    cmd = ["ssh", *SSH_OPTS, f"root@{ip}", f"nohup bash -c \"{script_content}\" >/dev/null 2>&1 & echo $!"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        pid = result.stdout.strip()
        return pid if pid.isdigit() else None
    except: return None

def kill_remote_pid(ip, pid):
    if pid: subprocess.run(["ssh", *SSH_OPTS, f"root@{ip}", f"kill -9 {pid}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

# ==========================================
# FLASK ROUTES & UI
# ==========================================

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Carrier Board Functional Testing</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Barlow:wght@400;600;700&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg: #0a0e14; --surface: #111720; --border: #1e2d3d;
      --accent: #00c8ff; --accent2: #00ff9d; --warn: #ffb347;
      --err: #ff4d4d; --text: #ccd6f6; --muted: #556070;
      --mono: 'Share Tech Mono', monospace; --sans: 'Barlow', sans-serif;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { background: var(--bg); color: var(--text); font-family: var(--sans); min-height: 100vh; }
    header { background: var(--surface); border-bottom: 1px solid var(--border); padding: 16px 28px; display: flex; align-items: center; gap: 18px; }
    .logo { font-family: var(--mono); font-size: 22px; color: var(--accent); letter-spacing: 2px; }
    .logo span { color: var(--accent2); }
    .controls { background: var(--surface); border-bottom: 1px solid var(--border); padding: 16px 28px; display: flex; align-items: center; gap: 18px; }
    input[type="text"], select { font-family: var(--mono); font-size: 15px; background: var(--bg); border: 1px solid var(--border); color: var(--text); padding: 8px 14px; border-radius: 4px; outline: none; }
    .btn { font-family: var(--sans); font-size: 14px; font-weight: 600; padding: 9px 20px; border: none; border-radius: 4px; cursor: pointer; transition: 0.2s;}
    .btn-start { background: var(--accent2); color: #000; }
    .btn-stop  { background: var(--warn); color: #000; }
    .btn-led   { background: #3498db; color: #fff; }
    .btn-rainbow { background: linear-gradient(90deg, #ff4d4d, #ffb347, #00ff9d, #00c8ff, #b14dff); color: white; text-shadow: 0 1px 2px #000; }
    .main { display: grid; grid-template-columns: 1fr 450px; height: calc(100vh - 130px); }
    .left-col { border-right: 1px solid var(--border); overflow-y: auto; }
    .card { background: var(--surface); margin: 20px; padding: 20px; border: 1px solid var(--border); border-radius: 8px; }
    .card-title { font-family: var(--mono); font-size: 18px; color: var(--accent); margin-bottom: 15px; text-transform: uppercase; border-bottom: 1px solid var(--border); padding-bottom: 10px; }
    #feed-container { width: 100%; height: 350px; background: #000; border-radius: 4px; display: flex; align-items: center; justify-content: center; margin-bottom: 15px;}
    #image { width: 100%; height: 100%; object-fit: contain; display: none; }
    .stats-bar { display: flex; gap: 15px; align-items: center; flex-wrap: wrap; }
    .gpio-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 15px; }
    .gpio-ind { padding: 15px; background: var(--bg); border: 1px solid var(--border); border-radius: 6px; text-align: center; font-family: var(--mono); transition: 0.1s;}
    .gpio-ind.active { background: var(--accent2); color: #000; font-weight: bold; box-shadow: 0 0 15px var(--accent2); }
    .log-container { flex: 1; overflow-y: auto; padding: 10px; font-family: var(--mono); font-size: 12px; background: var(--bg); }
    .log-line { padding: 4px 8px; border-bottom: 1px solid #1a222d; }
    .log-line.error { color: var(--err); }
  </style>
</head>
<body>
<header><div class="logo">Orin Nano <span>Board Tester</span></div></header>
<div class="controls">
  <select id="deviceSelect" onchange="document.getElementById('ipInput').value = this.value">
    <option value="">-- Discovered Devices --</option>
  </select>
  <input type="text" id="ipInput" placeholder="Target IP">
</div>
<div class="main">
  <div class="left-col">
    <div class="card">
      <div class="card-title">1. Camera Feed</div>
      <div id="feed-container">
        <img id="image">
        <div id="noStream" style="color:var(--muted)">Offline</div>
      </div>
      <div class="stats-bar">
        <button class="btn btn-start" id="camStart" onclick="startCamera()">▶ Start</button>
        <button class="btn btn-stop" id="camStop" onclick="stopCamera()" disabled>■ Stop</button>
        <div class="stat-item" style="margin-left: 20px;"><span id="fpsVal">0.0</span> FPS</div>
      </div>
    </div>
    
    <div class="card">
      <div class="card-title">2. RGB LED Driver</div>
      <div class="stats-bar">
          <button class="btn btn-led" id="ledBreathStart" onclick="startLED('start_breath')">☁ Breathing (White)</button>
          <button class="btn btn-rainbow" id="ledRainbowStart" onclick="startLED('start_rainbow')">🌈 Rainbow Cycle</button>
          <button class="btn btn-stop" id="ledStop" onclick="stopLED()" disabled>■ Stop</button>
      </div>
    </div>
    
    <div class="card">
      <div class="card-title">3. Alternating Relays (GPIO)</div>
      <div class="stats-bar">
          <button class="btn btn-start" id="gpioStart" onclick="startGPIO()">▶ Start Toggle</button>
          <button class="btn btn-stop" id="gpioStop" onclick="stopGPIO()" disabled>■ Stop</button>
      </div>
      <div class="gpio-grid">
        <div class="gpio-ind" id="ind-PP06">PP.06 (Relay 1)</div>
        <div class="gpio-ind" id="ind-PCC00">PCC.00 (Relay 2)</div>
      </div>
    </div>
  </div>
  <div class="right-col" style="display:flex; flex-direction:column;">
    <div class="card-title" style="padding: 20px; background:var(--surface); margin:0;">dmesg logs</div>
    <div id="logBox" class="log-container"></div>
  </div>
</div>
<script>
  let currentIp = null;
  let statsInt = null, dmesgInt = null, gpioInt = null;

  setInterval(async () => {
    try {
      const r = await fetch("/api/scanned");
      const devices = await r.json();
      const sel = document.getElementById("deviceSelect");
      devices.forEach(ip => {
        if(![...sel.options].some(o => o.value === ip)) sel.add(new Option(ip, ip));
      });
    } catch(e){}
  }, 3000);

  function startCamera() {
    const ip = document.getElementById("ipInput").value;
    if(!ip) return; currentIp = ip;
    const img = document.getElementById("image");
    img.src = `/video_feed?ip=${encodeURIComponent(ip)}`;
    img.style.display = "block";
    document.getElementById("noStream").style.display = "none";
    document.getElementById("camStart").disabled = true;
    document.getElementById("camStop").disabled = false;
    statsInt = setInterval(async () => {
      try {
        const r = await fetch("/fps"); const d = await r.json();
        document.getElementById("fpsVal").textContent = d.fps.toFixed(1);
      } catch(e){}
    }, 1000);
    dmesgInt = setInterval(async () => {
      try {
        const r = await fetch(`/dmesg?ip=${currentIp}`); const d = await r.json();
        document.getElementById("logBox").innerHTML = d.all_lines.map(l => `<div class="log-line">${l.text}</div>`).join("");
      } catch(e){}
    }, 5000);
  }

  function stopCamera() {
    document.getElementById("image").src = "";
    document.getElementById("image").style.display = "none";
    document.getElementById("noStream").style.display = "block";
    document.getElementById("camStart").disabled = false;
    document.getElementById("camStop").disabled = true;
    if(statsInt) clearInterval(statsInt);
    if(dmesgInt) clearInterval(dmesgInt);
  }

  async function startLED(mode) {
    const ip = document.getElementById("ipInput").value;
    if(!ip) return;
    document.getElementById("ledBreathStart").disabled = true;
    document.getElementById("ledRainbowStart").disabled = true;
    document.getElementById("ledStop").disabled = false;
    await fetch(`/api/led?ip=${ip}&state=${mode}`, {method:'POST'});
  }

  async function stopLED() {
    document.getElementById("ledBreathStart").disabled = false;
    document.getElementById("ledRainbowStart").disabled = false;
    document.getElementById("ledStop").disabled = true;
    await fetch(`/api/led?ip=${document.getElementById("ipInput").value}&state=stop`, {method:'POST'});
  }

  async function startGPIO() {
    const ip = document.getElementById("ipInput").value;
    if(!ip) return;
    document.getElementById("gpioStart").disabled = true;
    document.getElementById("gpioStop").disabled = false;
    await fetch(`/api/gpio?ip=${ip}&state=start`, {method:'POST'});
    let step = 0;
    gpioInt = setInterval(() => {
      document.getElementById("ind-PP06").classList.toggle('active', step % 2 === 0);
      document.getElementById("ind-PCC00").classList.toggle('active', step % 2 !== 0);
      step++;
    }, 1000);
  }

  async function stopGPIO() {
    document.getElementById("gpioStart").disabled = false;
    document.getElementById("gpioStop").disabled = true;
    if(gpioInt) { clearInterval(gpioInt); gpioInt = null; }
    document.getElementById("ind-PP06").classList.remove('active');
    document.getElementById("ind-PCC00").classList.remove('active');
    await fetch(`/api/gpio?ip=${document.getElementById("ipInput").value}&state=stop`, {method:'POST'});
  }
</script>
</body>
</html>
"""

@app.route("/")
def index(): return HTML_TEMPLATE

@app.route("/api/scanned")
def scanned_ips(): return jsonify(list(discovered_devices))

@app.route("/video_feed")
def video_feed():
    return Response(generate_frames(request.args.get("ip", "")), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route("/fps")
def get_fps(): return jsonify({"fps": current_fps})

@app.route("/dmesg")
def get_dmesg(): return jsonify(fetch_dmesg(request.args.get("ip", "")))

@app.route("/api/led", methods=['POST'])
def toggle_led():
    global remote_pids
    state, ip = request.args.get("state"), request.args.get("ip", "")
    
    if state == "start_breath" and ip:
        # Save Breathing Script Locally ensuring \n endings
        with open("leds.sh", "w", newline='\n') as f:
            f.write(ORIGINAL_LED_SCRIPT)
        
        subprocess.run(["scp", *SSH_OPTS[0:4], "leds.sh", f"root@{ip}:/tmp/leds.sh"], check=False)
        start_cmd = "chmod +x /tmp/leds.sh && /tmp/leds.sh LED0 start && /tmp/leds.sh LED1 start && /tmp/leds.sh LED2 start"
        subprocess.run(["ssh", *SSH_OPTS, f"root@{ip}", start_cmd])
        
    elif state == "start_rainbow" and ip:
        # Save Rainbow Script Locally ensuring \n endings
        with open("rainbow.sh", "w", newline='\n') as f:
            f.write(RAINBOW_LED_SCRIPT)
            
        subprocess.run(["scp", *SSH_OPTS[0:4], "rainbow.sh", f"root@{ip}:/tmp/rainbow.sh"], check=False)
        # We wrap this one in run_remote_script because it has a while true loop
        remote_pids["led"] = run_remote_script(ip, "chmod +x /tmp/rainbow.sh && /tmp/rainbow.sh")

    elif state == "stop" and ip:
        kill_remote_pid(ip, remote_pids["led"])
        remote_pids["led"] = None
        # Shut down driver explicitly
        stop_cmd="killall -9 rainbow.sh"
        subprocess.run(["ssh", *SSH_OPTS, f"root@{ip}", stop_cmd])
        
    return jsonify({"status": "ok"})

@app.route("/api/gpio", methods=['POST'])
def toggle_gpio():
    global remote_pids
    state, ip = request.args.get("state"), request.args.get("ip", "")
    if state == "start" and ip:
        gpio_script = """
        gpioset -t 1s,1s,0 PP.06=1
        gpioset -t 1s,1s,0 PCC.00=1
        """
        remote_pids["gpio"] = run_remote_script(ip, gpio_script)
    elif state == "stop" and ip:
        kill_remote_pid(ip, remote_pids["gpio"])
        remote_pids["gpio"] = None
        subprocess.run(["ssh", *SSH_OPTS, f"root@{ip}", "killall -9 gpioset"], stdout=subprocess.DEVNULL)
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, threaded=True)