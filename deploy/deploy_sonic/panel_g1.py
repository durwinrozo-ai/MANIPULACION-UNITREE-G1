#!/usr/bin/env python3
"""Panel de control web del Robot 3 cajas (G1 real y simulador MuJoCo).

Corre en TU PC (el que tiene .venv_teleop) y se abre en el navegador:

    cd $GR00T && source .venv_teleop/bin/activate
    python $REPO/deploy/deploy_sonic/panel_g1.py            # http://127.0.0.1:8080

Reemplaza abrir 5 terminales a mano: lanza/detiene cada proceso (camara del robot por ssh, emisor
de microfono, deploy SONIC, programa de vision, MuJoCo), muestra lo que ve el robot, tiene los botones
de las teclas (s, 0, 1/2/3, w, b, ESPACIO), el interruptor de voz y la parada de emergencia.
Solo escucha en 127.0.0.1. La contrasena del robot se guarda solo en memoria.
"""
import argparse
import collections
import json
import os
import pty
import re
import select
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
CFG_FILE = os.path.join(HERE, "panel_config.json")
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\r")

DEFAULT_CFG = {
    "gr00t": "/home/udirobotika/DURVVIN/SIM2SIM/blea/GR00T-WholeBodyControl",
    "repo_dir": os.path.abspath(os.path.join(HERE, "..", "..")),
    "venv": ".venv_teleop",
    "robot_user": "unitree", "robot_pc2": "192.168.123.164", "robot_pc1": "192.168.123.161",
    "pc_ip": "192.168.123.222", "iface": "enp131s0",
    "hsv_file": "~/tres-colores/hsv_real.json",
    "mic_nombre": "Insta", "udp_puerto": 5600,
    "stop_y": 0.93, "walk_speed": 0.3, "scan_ang_vel": 0.3,
    "voz_activacion": "zuu", "voz_umbral": 300, "voz_idioma": "es-CO",
    "usar_voz": True, "usar_mic": True, "dry_run": False,
    "vision_port": 8765, "modo": "real",
}


def load_cfg():
    cfg = dict(DEFAULT_CFG)
    try:
        with open(CFG_FILE) as f:
            cfg.update(json.load(f))
    except Exception:  # noqa: BLE001
        pass
    return cfg


CFG = load_cfg()
PASSWORD = {"v": ""}  # solo memoria


def save_cfg():
    with open(CFG_FILE, "w") as f:
        json.dump(CFG, f, indent=1)


# ---------------------------------------------------------------------------
# Gestor de procesos (cada uno con su pseudo-terminal, como una terminal real)
# ---------------------------------------------------------------------------
class Proc:
    def __init__(self, name, title):
        self.name, self.title = name, title
        self.cmd = ""
        self.p = None
        self.fd = None
        self.lines = collections.deque(maxlen=1500)
        self.total = 0
        self.state = "parado"  # parado | iniciando | listo | error
        self.t0 = 0.0
        self.ready_re = None
        self.error_re = None
        self.auto = []  # [(regex, texto_a_enviar, una_vez)]
        self._tail = ""
        self._last_pw = 0.0
        self._pw_sent = None
        self._pw_warn = None
        self.flags = {}
        self.lock = threading.Lock()

    def alive(self):
        return self.p is not None and self.p.poll() is None

    def start(self, cmd, ready=None, error=None, auto=None):
        if self.alive():
            return False
        self.cmd = cmd
        self.ready_re = re.compile(ready, re.I) if ready else None
        self.error_re = re.compile(error, re.I) if error else None
        self.auto = [[re.compile(r, re.I), t, once, False] for r, t, once in (auto or [])]
        self.flags = {}
        self._tail = ""
        self.state = "iniciando"
        self.t0 = time.time()
        master, slave = pty.openpty()
        env = dict(os.environ, TERM="dumb", PYTHONUNBUFFERED="1")
        self.p = subprocess.Popen(["bash", "-c", cmd], stdin=slave, stdout=slave, stderr=slave,
                                  preexec_fn=os.setsid, close_fds=True, env=env)
        os.close(slave)
        self.fd = master
        self._log(f"$ {cmd}")
        threading.Thread(target=self._reader, daemon=True).start()
        return True

    def _log(self, line):
        with self.lock:
            self.lines.append(line)
            self.total += 1

    def _reader(self):
        buf = ""
        while True:
            try:
                r, _, _ = select.select([self.fd], [], [], 0.3)
                if r:
                    data = os.read(self.fd, 4096)
                    if not data:
                        break
                    buf += ANSI.sub("", data.decode("utf-8", "replace"))
                    while "\n" in buf:
                        ln, buf = buf.split("\n", 1)
                        self._on_line(ln)
                    if buf:
                        self._on_partial(buf)
                else:
                    if buf:
                        self._on_partial(buf)  # prompt pendiente (p. ej. contrasena guardada despues)
                    if self.p.poll() is not None:
                        break
            except OSError:
                break
        if buf.strip():
            self._on_line(buf)
        rc = self.p.wait() if self.p else None
        self._log(f"[proceso terminado, codigo {rc}]")
        if self.state != "error":
            self.state = "parado" if rc in (0, -2, -15, 130, 143, None) or self.flags.get("stopping") else "error"
        try:
            os.close(self.fd)
        except OSError:
            pass
        self.fd = None

    def _on_partial(self, buf):
        # prompts sin salto de linea (password, y/n, SI)
        for a in self.auto:
            if a[0].search(buf) and not a[3]:
                self._send_auto(a, buf)
        if re.search(r"password.*:\s*$", buf, re.I):
            if PASSWORD["v"]:
                if self._pw_sent != buf:  # una vez por cada pregunta (evita bloqueos si la clave es incorrecta)
                    self._pw_sent = buf
                    self.send(PASSWORD["v"])
                    self._log("[panel] contrasena enviada")
            elif self._pw_warn != buf:
                self._pw_warn = buf
                self._log("[panel] EL ROBOT PIDE CONTRASENA: escribela en 'Contrasena del robot' y pulsa Guardar (se envia sola)")
        if re.search(r"Escribe SI", buf):
            self.flags["espera_si"] = True

    def _send_auto(self, a, text):
        if a[2]:
            a[3] = True
        self._log(f"[panel] respuesta automatica: {a[1]!r}")
        self.send(a[1])

    def _on_line(self, ln):
        self._log(ln)
        for a in self.auto:
            if a[0].search(ln) and not a[3]:
                self._send_auto(a, ln)
        if self.error_re and self.error_re.search(ln):
            self.flags["aviso"] = ln.strip()[:160]
            if self.state == "iniciando":
                self.state = "error"
        if self.ready_re and self.ready_re.search(ln) and self.state in ("iniciando", "error"):
            self.state = "listo"
        if self.name == "vision":
            if "comando start enviado" in ln or "start reenviado" in ln:
                self.flags["espera_si"] = False
            if ln.startswith("[Voz] oi:"):
                PHRASES.appendleft({"t": time.strftime("%H:%M:%S"), "txt": ln[10:].strip()})
        if self.name == "voz":
            if ln.startswith("[Voz] oi:"):
                PHRASES.appendleft({"t": time.strftime("%H:%M:%S"), "txt": ln[10:].strip()})

    def send(self, text, newline=True):
        if self.fd is None or not self.alive():
            return False
        os.write(self.fd, (text + ("\n" if newline else "")).encode())
        return True

    def stop(self, wait=6.0):
        if not self.alive():
            self.state = "parado" if self.state != "error" else self.state
            return
        self.flags["stopping"] = True
        pgid = os.getpgid(self.p.pid)
        for sig, t in ((signal.SIGINT, wait * 0.5), (signal.SIGTERM, wait * 0.3), (signal.SIGKILL, 2)):
            try:
                os.killpg(pgid, sig)
            except ProcessLookupError:
                break
            t_end = time.time() + t
            while time.time() < t_end and self.p.poll() is None:
                time.sleep(0.1)
            if self.p.poll() is not None:
                break
        self.state = "parado"

    def info(self):
        return {"name": self.name, "title": self.title, "state": self.state if (self.alive() or self.state == "error") else "parado",
                "alive": self.alive(), "uptime": round(time.time() - self.t0) if self.alive() else 0,
                "aviso": self.flags.get("aviso", ""), "espera_si": bool(self.flags.get("espera_si")) and self.alive()}


PHRASES = collections.deque(maxlen=12)
PROCS = {n: Proc(n, t) for n, t in [
    ("cam", "A · Cámara del robot (PC2)"), ("mic", "M · Micrófono del robot"),
    ("sim", "1 · Simulador MuJoCo"), ("deploy", "2 · Deploy SONIC"),
    ("vision", "3 · Programa de cajas"), ("voz", "V · Prueba de voz"),
    ("util", "U · Utilidades (scp, calibrar)")]}


def sh_q(s):
    return shlex.quote(str(s))


def ssh_cmd(remote):
    c = CFG
    # ssh no interactivo no carga ~/.bashrc: teleimager-server vive en ~/.local/bin
    remote = 'export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"; ' + remote
    return (f"ssh -tt -o StrictHostKeyChecking=accept-new -o ConnectTimeout=8 "
            f"{c['robot_user']}@{c['robot_pc2']} {sh_q(remote)}")


def venv_prefix():
    return f"cd {sh_q(CFG['gr00t'])} && source {sh_q(CFG['venv'])}/bin/activate && "


def start_proc(name, real=None):
    c = CFG
    P = PROCS[name]
    real = c.get("modo", "real") == "real" if real is None else real
    sd = os.path.join(c["repo_dir"], "deploy", "deploy_sonic")
    if name == "cam":
        return P.start(ssh_cmd("bash ~/start_camera.sh"), ready=r"head_camera is ready|Running\.\.\.",
                       error=r"busy|Failed to initialize|Permission denied|No route|timed out")
    if name == "mic":
        remote = (f"python3 ~/mic_stream_g1.py --nombre {c['mic_nombre']} --destino {c['pc_ip']} "
                  f"--puerto {c['udp_puerto']}")
        return P.start(ssh_cmd(remote), ready=r"enviados \d+ bloques", error=r"No such file|Permission denied|error|no hay|No se encontro")
    if name == "sim":
        return P.start(venv_prefix() + f"python {sh_q(sd)}/run_sim_3colores.py",
                       ready=r"Sensor server running", error=r"Traceback|Error")
    if name == "deploy":
        iface = c["iface"] if real else "sim"
        return P.start(f"cd {sh_q(c['gr00t'])}/gear_sonic_deploy && source scripts/setup_env.sh && "
                       f"./deploy.sh --input-type zmq_manager {iface}",
                       ready=r"Init Done", error=r"\[ERROR\]|Lost LowState|Address already in use",
                       auto=[(r"\(y/n\)|\[y/n\]|y/N|Y/n|\(y\)|Press y|enter y", "y", True)])
    if name == "vision":
        a = [f"--panel-port {c['vision_port']}", "--no-gui", f"--stop-y {c['stop_y']}",
             f"--walk-speed {c['walk_speed']}"]
        if real:
            a += ["--source g1", "--real", f"--hsv-file {sh_q(os.path.expanduser(c['hsv_file']))}",
                  f"--scan-ang-vel {c['scan_ang_vel']}"]
        if c.get("dry_run"):
            a.append("--dry-run")
        if c.get("usar_voz"):
            a += ["--voz udp", f"--voz-puerto {c['udp_puerto']}", f"--voz-activacion {sh_q(c['voz_activacion'])}",
                  f"--voz-umbral {c['voz_umbral']}", f"--voz-idioma {c['voz_idioma']}"]
        return P.start(venv_prefix() + f"python {sh_q(sd)}/deploy_sonic_vision.py " + " ".join(a),
                       ready=r"comando start enviado|start reenviado", error=r"\[ERROR\]|Traceback|Address already in use")
    if name == "voz":
        return P.start(venv_prefix() + f"python {sh_q(sd)}/voz_cajas.py --fuente udp --puerto {c['udp_puerto']} "
                       f"--activacion {sh_q(c['voz_activacion'])} --umbral {c['voz_umbral']}",
                       ready=r"escuchando", error=r"Traceback|ERROR")
    return False


def run_util(kind):
    c = CFG
    P = PROCS["util"]
    sd = os.path.join(c["repo_dir"], "deploy", "deploy_sonic")
    if kind == "scp":
        files = f"{sh_q(sd)}/mic_stream_g1.py {sh_q(sd)}/robot/start_camera.sh"
        cmd = (f"scp -o StrictHostKeyChecking=accept-new {files} {c['robot_user']}@{c['robot_pc2']}:~/ "
               f"&& echo '[OK] archivos copiados al robot (~/mic_stream_g1.py, ~/start_camera.sh)'")
        return P.start(cmd, ready=r"\[OK\]", error=r"Permission denied|No such file|lost connection|timed out")
    if kind == "calibrar":
        return P.start(venv_prefix() + f"python {sh_q(sd)}/deploy_sonic_vision.py --source g1 --calibrar "
                       f"--hsv-file {sh_q(os.path.expanduser(c['hsv_file']))}", ready=r"esperando imagen|Camara", error=r"Traceback")
    if kind == "liberar5556":
        return P.start("fuser -v 5556/tcp; fuser -k 5556/tcp; echo '[OK] puerto 5556 liberado'", ready=r"\[OK\]")
    if kind == "videofree":
        return P.start(ssh_cmd("sudo fuser -v /dev/video* 2>&1 | head -20; echo '[OK] listo'"), ready=r"\[OK\]",
                       error=r"Permission denied")
    return False


# ---------------------------------------------------------------------------
# Comprobaciones de red (ping / interfaz)
# ---------------------------------------------------------------------------
CHECKS = {"pc1": None, "pc2": None, "iface": None, "ssh": None}


def _ping(ip):
    try:
        r = subprocess.run(["ping", "-c", "1", "-W", "1", ip], capture_output=True, text=True, timeout=3)
        m = re.search(r"time=([\d.]+)", r.stdout)
        return float(m.group(1)) if r.returncode == 0 and m else None
    except Exception:  # noqa: BLE001
        return None


def _port_open(ip, port, t=0.8):
    try:
        with socket.create_connection((ip, port), timeout=t):
            return True
    except OSError:
        return False


def checker():
    while True:
        try:
            CHECKS["pc1"] = _ping(CFG["robot_pc1"])
            CHECKS["pc2"] = _ping(CFG["robot_pc2"])
            CHECKS["ssh"] = _port_open(CFG["robot_pc2"], 22)
            out = subprocess.run(["ip", "-br", "addr", "show", CFG["iface"]], capture_output=True, text=True).stdout.strip()
            CHECKS["iface"] = out or None
        except Exception:  # noqa: BLE001
            pass
        time.sleep(3)


# ---------------------------------------------------------------------------
# Camara cruda del panel (antes de lanzar el programa de vision)
# ---------------------------------------------------------------------------
class RawCam:
    def __init__(self):
        self.rx = None
        self.mode = None
        self.lock = threading.Lock()

    def ensure(self, real):
        import deploy_sonic_vision as dv  # misma carpeta
        with self.lock:
            mode = "real" if real else "sim"
            if self.rx is not None and self.mode == mode:
                return
            if self.rx is not None:
                self.rx.stop = True
            self.mode = mode
            self.rx = dv.G1Receiver(CFG["robot_pc2"], 55555) if real else dv.SimReceiver("127.0.0.1", 5555, "ego_view")
            self.rx.start()

    def stop(self):
        with self.lock:
            if self.rx is not None:
                self.rx.stop = True
            self.rx = None

    def jpeg(self):
        import cv2
        if self.rx is None:
            return None
        fr, ft = self.rx.latest()
        if fr is None:
            return None
        bgr = cv2.cvtColor(fr, cv2.COLOR_RGB2BGR)
        bgr = cv2.resize(bgr, (640, 480))
        cv2.putText(bgr, "camara sin procesar (programa de cajas apagado)", (8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
        return cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 70])[1].tobytes()

    def age(self):
        if self.rx is None:
            return None
        return round(time.time() - self.rx.latest()[1], 1) if self.rx.latest()[0] is not None else None


RAW = RawCam()


# ---------------------------------------------------------------------------
# Secuencias de arranque / cierre
# ---------------------------------------------------------------------------
SEQ = {"running": False, "steps": [], "msg": "", "cancel": False}


def _wait(name, states=("listo",), timeout=60):
    t_end = time.time() + timeout
    P = PROCS[name]
    while time.time() < t_end and not SEQ["cancel"]:
        if P.state in states:
            return True
        if P.state == "error" or (not P.alive() and time.time() - P.t0 > 2):
            return False
        time.sleep(0.3)
    return False


def _step(i, state):
    SEQ["steps"][i]["state"] = state


def run_start_all(real):
    SEQ.update(running=True, cancel=False, msg="")
    plan = []
    if real:
        plan.append(("Cámara del robot (A)", "cam", 90))
        if CFG["usar_mic"]:
            plan.append(("Micrófono (M)", "mic", 30))
        plan.append(("Deploy SONIC (2)", "deploy", 120))
    else:
        plan += [("Simulador MuJoCo (1)", "sim", 120), ("Deploy SONIC (2)", "deploy", 120)]
    plan.append(("Programa de cajas (3)", "vision", 40))
    SEQ["steps"] = [{"txt": t, "state": "pendiente"} for t, _, _ in plan]
    try:
        for i, (txt, name, tmo) in enumerate(plan):
            if SEQ["cancel"]:
                break
            _step(i, "en curso")
            if not PROCS[name].alive():
                start_proc(name, real)
            if name == "vision" and real:
                # el programa pide SI: lo confirma la persona en el panel (no se automatiza)
                SEQ["msg"] = "Esperando tu confirmación 'SI' en el panel (caja roja de seguridad)."
                t_end = time.time() + 40
                while time.time() < t_end and not PROCS["vision"].flags.get("espera_si") and PROCS["vision"].alive() and not SEQ["cancel"]:
                    time.sleep(0.3)
                if PROCS["vision"].flags.get("espera_si"):
                    _step(i, "espera SI")
                    t_end = time.time() + 300
                    while PROCS["vision"].flags.get("espera_si") and time.time() < t_end and not SEQ["cancel"]:
                        time.sleep(0.3)
                    SEQ["msg"] = ""
            ok = _wait(name, timeout=tmo)
            if name == "vision" and PROCS["vision"].state == "listo":
                ok = True
            _step(i, "listo" if ok else "ERROR")
            if not ok:
                SEQ["msg"] = f"Falló: {txt}. Mira el registro de esa terminal."
                break
        else:
            SEQ["msg"] = "Todo en marcha."
    finally:
        SEQ["running"] = False


def run_stop_all():
    SEQ.update(running=True, cancel=True)
    time.sleep(0.2)
    SEQ.update(cancel=False, msg="")
    order = [("1) ESPACIO (robot quieto)", None), ("2) Programa de cajas (q)", "vision"), ("3) Deploy SONIC", "deploy"),
             ("4) Micrófono", "mic"), ("5) Cámara del robot", "cam"), ("6) Simulador / pruebas", "sim")]
    SEQ["steps"] = [{"txt": t, "state": "pendiente"} for t, _ in order]
    try:
        for i, (txt, name) in enumerate(order):
            _step(i, "en curso")
            if name is None:
                vision_key(" ")
                time.sleep(0.6)
            else:
                P = PROCS[name]
                if name == "vision":
                    vision_key("q")
                    t_end = time.time() + 3
                    while P.alive() and time.time() < t_end:
                        time.sleep(0.2)
                P.stop()
                if name == "sim":
                    PROCS["voz"].stop()
                    PROCS["util"].stop()
            _step(i, "listo")
        RAW.stop()
        SEQ["msg"] = "Todo detenido. Antes de relanzar: puerto 5556 libre y /dev/video* libre (botones de Utilidades)."
    finally:
        SEQ["running"] = False


# ---------------------------------------------------------------------------
# Puente con el programa de vision
# ---------------------------------------------------------------------------
def vision_url(path):
    return f"http://127.0.0.1:{CFG['vision_port']}{path}"


def vision_status():
    if not PROCS["vision"].alive():
        return None
    try:
        with urllib.request.urlopen(vision_url("/status"), timeout=0.4) as r:
            d = json.loads(r.read())
            return d or None
    except Exception:  # noqa: BLE001
        return None


def vision_key(k):
    try:
        req = urllib.request.Request(vision_url(f"/key?k={'space' if k == ' ' else k}"), method="POST")
        with urllib.request.urlopen(req, timeout=0.6):
            return True
    except Exception:  # noqa: BLE001
        return False


def vision_voz(on):
    try:
        req = urllib.request.Request(vision_url(f"/voz?on={1 if on else 0}"), method="POST")
        with urllib.request.urlopen(req, timeout=0.6):
            return True
    except Exception:  # noqa: BLE001
        return False


def state_json(log=None, since=0):
    vs = vision_status()
    out = {
        "cfg": {k: v for k, v in CFG.items()}, "procs": [p.info() for p in PROCS.values()],
        "checks": CHECKS, "vision": vs, "phrases": list(PHRASES), "seq": SEQ,
        "pw_set": bool(PASSWORD["v"]), "raw_age": RAW.age(), "vision_port": CFG["vision_port"],
    }
    if log in PROCS:
        P = PROCS[log]
        with P.lock:
            lines = list(P.lines)
            total = P.total
        n_new = max(0, total - since)
        out["log"] = {"name": log, "total": total, "lines": lines[-n_new:] if n_new else [],
                      "reset": since > total or n_new > len(lines)}
    return out


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            b = open(os.path.join(HERE, "panel_g1.html"), "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        elif u.path == "/api/state":
            self._json(state_json(q.get("log", [None])[0], int(q.get("since", ["0"])[0])))
        elif u.path == "/cam.mjpg":
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            try:
                while True:
                    jpg = RAW.jpeg()
                    if jpg:
                        self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(jpg))
                        self.wfile.write(jpg + b"\r\n")
                    time.sleep(0.08)
            except (BrokenPipeError, ConnectionResetError, OSError):
                return
        else:
            self._json({"error": "no existe"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:  # noqa: BLE001
            body = {}
        p = u.path
        real = CFG.get("modo", "real") == "real"
        if p == "/api/proc":
            name, act = body.get("name"), body.get("action")
            if name not in PROCS:
                return self._json({"ok": False}, 400)
            if act == "start":
                if name == "util":
                    ok = run_util(body.get("kind", ""))
                else:
                    ok = start_proc(name, real)
                return self._json({"ok": bool(ok)})
            if act == "stop":
                threading.Thread(target=PROCS[name].stop, daemon=True).start()
                return self._json({"ok": True})
            if act == "send":
                return self._json({"ok": PROCS[name].send(str(body.get("text", "")), body.get("newline", True))})
        elif p == "/api/key":
            return self._json({"ok": vision_key(str(body.get("k", ""))[:1] or "")})
        elif p == "/api/emergencia":
            vision_key(" ")
            ok = PROCS["deploy"].send("O", newline=False)
            return self._json({"ok": True, "deploy": ok})
        elif p == "/api/voz":
            return self._json({"ok": vision_voz(bool(body.get("on")))})
        elif p == "/api/config":
            for k, v in body.items():
                if k in CFG:
                    CFG[k] = type(DEFAULT_CFG[k])(v) if k in DEFAULT_CFG and not isinstance(DEFAULT_CFG[k], bool) else bool(v)
            if "modo" in body:
                CFG["modo"] = "real" if body["modo"] == "real" else "sim"
            save_cfg()
            return self._json({"ok": True})
        elif p == "/api/password":
            PASSWORD["v"] = str(body.get("pw", ""))
            return self._json({"ok": True})
        elif p == "/api/confirmar_si":
            ok = PROCS["vision"].send("SI")
            PROCS["vision"].flags["espera_si"] = False
            return self._json({"ok": ok})
        elif p == "/api/camview":
            if body.get("on"):
                RAW.ensure(real)
            else:
                RAW.stop()
            return self._json({"ok": True})
        elif p == "/api/seq":
            if body.get("action") == "start" and not SEQ["running"]:
                threading.Thread(target=run_start_all, args=(real,), daemon=True).start()
            elif body.get("action") == "stop" and not SEQ["running"]:
                threading.Thread(target=run_stop_all, daemon=True).start()
            elif body.get("action") == "cancel":
                SEQ["cancel"] = True
            return self._json({"ok": True})
        self._json({"error": "no existe"}, 404)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8080)
    a = ap.parse_args()
    sys.path.insert(0, HERE)
    CFG.setdefault("modo", "real")
    threading.Thread(target=checker, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), H)
    srv.daemon_threads = True
    print(f"Panel en http://127.0.0.1:{a.port}   (Ctrl+C para cerrar; detiene los procesos lanzados desde aqui)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for n in ("vision", "deploy", "mic", "cam", "sim", "voz", "util"):
            PROCS[n].stop(3)
        RAW.stop()


if __name__ == "__main__":
    main()
