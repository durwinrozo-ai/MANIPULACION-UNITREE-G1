#!/usr/bin/env python3
"""Busqueda y aproximacion a cajas de colores con el G1 de 29 GDL (politica SONIC).

Equivalente de deploy_mujoco_vision.py (12 GDL / unitree_rl_gym) para el entorno
GR00T-WholeBodyControl. Funciona en dos modos:

  SIMULADOR (por defecto)
    run_sim_3colores.py ──imagen ZMQ :5555──▶ este programa ──planner ZMQ :5556──▶ deploy.sh sim

  ROBOT REAL  (--source g1 --real)
    teleimager-server (PC2) ──JPEG ZMQ :55555──▶ este programa ──planner :5556──▶ deploy.sh <interfaz>

Este programa hace de "operador": recibe la camara, detecta rojo/verde/azul y publica
mensajes `planner` (modo, movimiento, facing) con el mismo formato que
gear_sonic/scripts/pico_manager_thread_server.py. No usa Pico 4.

Teclas (con foco en la ventana "Lo que ve el robot"; no distingue mayusculas):
    s  girar en el sitio inspeccionando        0  quieto (cancela todo)
    1  buscar y caminar a la caja roja         2  verde        3  azul
    ESPACIO o x  PARAR (IDLE inmediato)        q  salir (deja al robot quieto)
    w  prueba de marcha: camina recto 3 s sin usar la vision (aisla el problema)
    Voz (--voz g1|pc): "zuu, busca el rojo/verde/azul", "zuu, inspecciona", "zuu, quieto"; "para"/"alto" = PARAR
    b  reenviar el comando `start` (en --real: pulsar dos veces en 3 s)

Robot real: la parada de emergencia de verdad es la tecla O en la terminal del deploy
o el mando de Unitree; ESPACIO solo detiene el planner.

Calibracion de colores (sin enviar nada al robot):
    python deploy_sonic_vision.py --source g1 --calibrar
  Clic en la imagen = imprime el HSV del pixel. Rangos propios con --hsv-file (JSON):
    {"red": [[[0,130,70],[10,255,255]], [[170,130,70],[180,255,255]]],
     "green": [[[40,80,50],[85,255,255]]], "blue": [[[95,110,50],[130,255,255]]]}
"""
import argparse
import base64
import http.server
import json
import math
import queue
import struct
import sys
import threading
import time

import cv2
import msgpack
import numpy as np
import zmq

HEADER_SIZE = 1280  # debe coincidir con ZMQPackedMessageSubscriber::HEADER_SIZE

# LocomotionMode (localmotion_kplanner.hpp)
MODE_IDLE = 0
MODE_SLOW_WALK = 1  # 0.1 - 0.8 m/s

TARGET_STOP, TARGET_SCAN, TARGET_RED, TARGET_GREEN, TARGET_BLUE = -1, 0, 1, 2, 3
TARGET_NAMES = {-1: "QUIETO", 0: "INSPECCIONANDO", 1: "ROJO", 2: "VERDE", 3: "AZUL"}

REAL_MAX_WALK = 0.3  # m/s, tope en --real mientras no se valide el robot


# ----------------------------------------------------------------------------
# Mensajes ZMQ hacia SONIC (identicos byte a byte a tests/test_zmq_manager.py)
# ----------------------------------------------------------------------------
def _pack(topic: bytes, fields, data: bytes) -> bytes:
    header = {"v": 1, "endian": "le", "count": 1, "fields": fields}
    hj = json.dumps(header).encode("utf-8")
    return topic + hj + b"\x00" * (HEADER_SIZE - len(hj)) + data


def build_command(start: bool, stop: bool, planner: bool) -> bytes:
    fields = [{"name": n, "dtype": "u8", "shape": [1]} for n in ("start", "stop", "planner")]
    data = struct.pack("BBB", int(start), int(stop), int(planner))
    return _pack(b"command", fields, data)


def build_planner(mode: int, movement, facing, speed: float = -1.0, height: float = -1.0) -> bytes:
    fields = [
        {"name": "mode", "dtype": "i32", "shape": [1]},
        {"name": "movement", "dtype": "f32", "shape": [3]},
        {"name": "facing", "dtype": "f32", "shape": [3]},
        {"name": "speed", "dtype": "f32", "shape": [1]},
        {"name": "height", "dtype": "f32", "shape": [1]},
    ]
    data = struct.pack("<i", int(mode))
    data += struct.pack("<fff", *[float(v) for v in movement])
    data += struct.pack("<fff", *[float(v) for v in facing])
    data += struct.pack("<f", float(speed)) + struct.pack("<f", float(height))
    return _pack(b"planner", fields, data)


# ----------------------------------------------------------------------------
# Vision: mascaras de color
# ----------------------------------------------------------------------------
# Simulador: colores puros de MuJoCo (RGB)
RGB_RULES = {
    TARGET_RED: lambda r, g, b: (r > 150) & (g < 80) & (b < 80),
    TARGET_GREEN: lambda r, g, b: (g > 150) & (r < 80) & (b < 80),
    TARGET_BLUE: lambda r, g, b: (b > 150) & (r < 80) & (g < 80),
}

# Camara real: rangos HSV de OpenCV (H 0-180, S y V 0-255). Punto de partida:
# calibrar con --calibrar y las cajas reales, con la luz del lugar de trabajo.
DEFAULT_HSV = {
    TARGET_RED: [((0, 130, 70), (10, 255, 255)), ((170, 130, 70), (180, 255, 255))],
    TARGET_GREEN: [((40, 80, 50), (85, 255, 255))],
    TARGET_BLUE: [((95, 110, 50), (130, 255, 255))],
}
_NAME2T = {"red": TARGET_RED, "green": TARGET_GREEN, "blue": TARGET_BLUE}


def load_hsv_rules(path):
    rules = {k: list(v) for k, v in DEFAULT_HSV.items()}
    if path:
        with open(path) as f:
            data = json.load(f)
        for name, ranges in data.items():
            rules[_NAME2T[name]] = [(tuple(lo), tuple(hi)) for lo, hi in ranges]
    return rules


def color_mask(img_rgb, target, mode, hsv_rules, hsv=None):
    """Mascara booleana del color `target`. mode: 'rgb' (simulador) o 'hsv' (real)."""
    if mode == "rgb":
        r = img_rgb[:, :, 0].astype(np.int32)
        g = img_rgb[:, :, 1].astype(np.int32)
        b = img_rgb[:, :, 2].astype(np.int32)
        return RGB_RULES[target](r, g, b)
    if hsv is None:
        hsv = cv2.cvtColor(np.ascontiguousarray(img_rgb), cv2.COLOR_RGB2HSV)
    m = np.zeros(hsv.shape[:2], np.uint8)
    for lo, hi in hsv_rules[target]:
        m |= cv2.inRange(hsv, np.array(lo, np.uint8), np.array(hi, np.uint8))
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    return m > 0


def mask_stats(mask, min_pixels, largest=False):
    """(detectado, error_x, y_min, y_max) a partir de una mascara booleana.

    error_x: -1 (izquierda) .. +1 (derecha); positivo = el color esta a la derecha.
    y_max:   fila inferior del color (0 arriba .. 1 abajo). Crece al acercarse.
    largest: usa solo el componente conexo mas grande (descarta manchas sueltas).
    """
    h, w = mask.shape[:2]
    if largest and int(mask.sum()) >= min_pixels:
        n, labels, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=8)
        if n > 1:
            k = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
            mask = labels == k
    if int(mask.sum()) < min_pixels:
        return (False, 0.0, 0.0, 0.0), mask
    ys, xs = np.nonzero(mask)
    err = (xs.mean() - w / 2.0) / (w / 2.0)
    return (True, float(err), float(ys.min() / h), float(ys.max() / h)), mask


def detect_color(img_rgb, target, mode, hsv_rules, min_pixels, hsv=None):
    """Devuelve (det, mascara) con det = (detectado, err, y_min, y_max)."""
    mask = color_mask(img_rgb, target, mode, hsv_rules, hsv)
    return mask_stats(mask, min_pixels, largest=(mode == "hsv"))


# ----------------------------------------------------------------------------
# Vision: recepcion de imagen
# ----------------------------------------------------------------------------
class _Receiver(threading.Thread):
    conflate = True

    def __init__(self, host, port, swap_rb=False):
        super().__init__(daemon=True)
        self.host, self.port, self.swap_rb = host, port, swap_rb
        self.lock = threading.Lock()
        self.frame = None
        self.frame_time = 0.0
        self.count = 0
        self.stop = False

    def decode(self, parts):  # -> imagen RGB (uint8, HxWx3) o None
        raise NotImplementedError

    def run(self):
        ctx = zmq.Context.instance()
        sock = ctx.socket(zmq.SUB)
        if self.conflate:
            sock.setsockopt(zmq.CONFLATE, 1)  # solo la imagen mas reciente
        else:
            sock.setsockopt(zmq.RCVHWM, 2)
        sock.setsockopt_string(zmq.SUBSCRIBE, "")
        sock.setsockopt(zmq.RCVTIMEO, 500)
        sock.connect(f"tcp://{self.host}:{self.port}")
        while not self.stop:
            try:
                parts = sock.recv_multipart()
            except zmq.Again:
                continue
            try:
                img = self.decode(parts)
                if img is None:
                    continue
                if self.swap_rb:
                    img = img[:, :, ::-1]
                img = np.ascontiguousarray(img)
                with self.lock:
                    self.frame = img
                    self.frame_time = time.time()
                    self.count += 1
            except Exception as e:  # noqa: BLE001
                print(f"[Vision] mensaje de imagen invalido: {e}")
        sock.close(0)

    def latest(self):
        with self.lock:
            return self.frame, self.frame_time


class SimReceiver(_Receiver):
    """Camara del simulador: msgpack {'images': {clave: JPEG base64}} (la imagen ya es RGB)."""

    def __init__(self, host, port, key, swap_rb=False):
        super().__init__(host, port, swap_rb)
        self.key = key

    def decode(self, parts):
        msg = msgpack.unpackb(parts[0], raw=False)
        images = msg["images"]
        val = images[self.key] if self.key in images else next(iter(images.values()))
        if isinstance(val, str):
            val = base64.b64decode(val)
        return cv2.imdecode(np.frombuffer(val, np.uint8), cv2.IMREAD_COLOR)


class G1Receiver(_Receiver):
    """Camara del G1 real (teleimager-server, ZMQ PUB :55555, JPEG).

    No depende del formato exacto: busca el marcador JPEG (FF D8) dentro del mensaje
    (cruda, con cabecera o multiparte). Si la imagen es estereo (ancho >= 2.2x alto)
    usa la mitad izquierda. OpenCV decodifica en BGR y aqui se pasa a RGB.
    """

    conflate = False  # CONFLATE no admite mensajes multiparte

    def __init__(self, host, port, swap_rb=False):
        super().__init__(host, port, swap_rb)
        self._diag = True

    def decode(self, parts):
        if self._diag:
            self._diag = False
            desc = ", ".join(f"{len(p)} B [{p[:6].hex()}]" for p in parts)
            print(f"[Camara] primer mensaje: {len(parts)} parte(s): {desc}")
        for p in sorted(parts, key=len, reverse=True):
            i = p.find(b"\xff\xd8")
            if i < 0:
                continue
            img = cv2.imdecode(np.frombuffer(p[i:], np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                continue
            h, w = img.shape[:2]
            if w >= 2.2 * h:
                img = img[:, : w // 2]
            return img[:, :, ::-1]  # BGR -> RGB
        return None


# ----------------------------------------------------------------------------
# Navegacion (equivalente de compute_cmd_from_vision del programa de 12 GDL)
# ----------------------------------------------------------------------------
class Navigator:
    def __init__(self, args):
        self.a = args
        self.target = TARGET_STOP
        self.theta = 0.0  # rumbo deseado (rad), 0 = +x del mundo; positivo = izquierda
        self.reset_state()

    def reset_state(self):
        self.arrived = False
        self.arrive_ticks = 0
        self.err_s = 0.0
        self.last_seen = -1e9
        self.last_ymax = 0.0

    def set_target(self, t, force=False):
        # force=True: volver a elegir el mismo color reinicia la llegada ("LLEGO" ya no queda bloqueado)
        if t != self.target or force:
            self.target = t
            self.reset_state()

    def step(self, det, now, dt):
        """det = (detectado, err, ymin, ymax) del color elegido (o None).
        Devuelve (mode, movement, facing, speed, etiqueta_estado)."""
        a = self.a
        facing = (math.cos(self.theta), math.sin(self.theta), 0.0)

        def idle(label):
            return MODE_IDLE, (0.0, 0.0, 0.0), (math.cos(self.theta), math.sin(self.theta), 0.0), -1.0, label

        if self.target == TARGET_STOP:
            return idle("quieto")

        def scan():
            self.theta += a.scan_ang_vel * dt
            return idle("buscando")

        if self.target == TARGET_SCAN:
            return scan()

        if self.arrived:
            return idle("LLEGO")

        detected = det is not None and det[0]
        if detected:
            _, err, _, ymax = det
            self.last_seen = now
            self.last_ymax = ymax
            self.err_s = self.err_s * (1 - a.alpha_err) + err * a.alpha_err
            if ymax >= a.stop_y:
                self.arrive_ticks += 1
                if self.arrive_ticks >= a.arrive_ticks:
                    self.arrived = True
                    return idle("LLEGO")
            else:
                self.arrive_ticks = 0
        else:
            self.arrive_ticks = 0
            if now - self.last_seen > a.lost_hold:
                return scan()
            # perdida breve: seguir recto sin corregir el rumbo
            return self._walk("perdido (recto)", 0.0, facing_only=True)

        # correccion de rumbo: objeto a la derecha (err>0) -> girar a la derecha (theta baja)
        rate = float(np.clip(-a.k_turn * self.err_s, -a.max_turn, a.max_turn))
        self.theta += rate * dt

        if abs(self.err_s) < a.center_tol:
            return self._walk("avanzando", rate)
        return idle("centrando")

    def _walk(self, label, rate, facing_only=False):
        a = self.a
        speed = a.walk_speed
        if self.last_ymax > a.slow_y:  # frenado suave cerca de la caja
            speed = a.slow_speed
        speed = float(np.clip(speed, 0.1, 0.8))
        f = (math.cos(self.theta), math.sin(self.theta), 0.0)
        return MODE_SLOW_WALK, f, f, speed, label


# ----------------------------------------------------------------------------
def draw_overlay(img_rgb, nav, det, label, hz, mask=None, real=False):
    out = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2BGR)
    out = cv2.resize(out, (640, 480), interpolation=cv2.INTER_NEAREST)
    if mask is not None and mask.any():
        m8 = cv2.resize(mask.astype(np.uint8) * 255, (640, 480), interpolation=cv2.INTER_NEAREST)
        cnts, _ = cv2.findContours(m8, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        cv2.drawContours(out, cnts, -1, (0, 255, 255), 2)
    tag = "REAL" if real else "SIM"
    txt = f"[{tag}] {TARGET_NAMES[nav.target]} | {label} | theta={math.degrees(nav.theta):+.0f} deg | {hz:.0f} Hz"
    cv2.putText(out, txt, (8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 2)
    if det is not None and det[0]:
        cv2.putText(out, f"err={det[1]:+.2f} y_max={det[3]:.2f}", (8, 46),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 255), 2)
        yline = int(det[3] * 480)
        cv2.line(out, (0, yline), (640, yline), (0, 255, 255), 1)
    return out


class PanelBridge:
    """Servidor HTTP local (solo 127.0.0.1) para el panel web panel_g1.py:
    GET /video.mjpg (lo que ve el robot), GET /status, POST /key?k=, POST /voz?on=0|1."""

    def __init__(self, port):
        self.port = port
        self.keys = queue.Queue()
        self.voz_enabled = True
        self.status = {}
        self._jpg = None
        self._seq = 0
        self._cv = threading.Condition()
        self._t_push = 0.0

    def push(self, bgr, min_dt=0.066):
        now = time.time()
        if now - self._t_push < min_dt:
            return
        self._t_push = now
        ok, buf = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 72])
        if ok:
            with self._cv:
                self._jpg = buf.tobytes()
                self._seq += 1
                self._cv.notify_all()

    def start(self):
        bridge = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):  # silencio
                pass

            def _json(self, obj, code=200):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path.startswith("/status"):
                    return self._json(bridge.status)
                if self.path.startswith("/video.mjpg"):
                    self.send_response(200)
                    self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    last = -1
                    try:
                        while True:
                            with bridge._cv:
                                bridge._cv.wait_for(lambda: bridge._seq != last, timeout=2.0)
                                jpg, last = bridge._jpg, bridge._seq
                            if jpg is None:
                                continue
                            self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(jpg))
                            self.wfile.write(jpg + b"\r\n")
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        return
                self._json({"error": "no existe"}, 404)

            def do_POST(self):
                from urllib.parse import parse_qs, urlparse
                u = urlparse(self.path)
                q = parse_qs(u.query)
                if u.path == "/key":
                    k = q.get("k", [""])[0]
                    if k == "space":
                        k = " "
                    if len(k) == 1:
                        bridge.keys.put(k.lower())
                        return self._json({"ok": True, "k": k})
                    return self._json({"ok": False}, 400)
                if u.path == "/voz":
                    bridge.voz_enabled = q.get("on", ["1"])[0] == "1"
                    return self._json({"ok": True, "voz_enabled": bridge.voz_enabled})
                self._json({"error": "no existe"}, 404)

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), H)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        print(f"[Panel] puente HTTP en http://127.0.0.1:{self.port} (video.mjpg, status, key, voz)")
        return self


def calibrate(rx, args, hsv_rules):
    """Muestra la camara y las mascaras de rojo/verde/azul. No publica nada."""
    win = "Calibracion (clic = HSV del pixel | p = guardar | q = salir)"
    state = {"pt": None}
    s = 0.6  # escala de cada panel

    def on_mouse(ev, x, y, flags, param):
        if ev == cv2.EVENT_LBUTTONDOWN:
            state["pt"] = (int(x / s), int(y / s))

    cv2.namedWindow(win)
    cv2.setMouseCallback(win, on_mouse)
    print("[Calibrar] esperando imagen...")
    last = ""
    while True:
        frame, ft = rx.latest()
        if frame is None:
            cv2.imshow(win, np.zeros((480, 640, 3), np.uint8))
            if (cv2.waitKey(50) & 0xFF) == ord("q"):
                break
            continue
        hsv = cv2.cvtColor(frame, cv2.COLOR_RGB2HSV)
        size = (int(640 * s), int(480 * s))
        base = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_RGB2BGR), size)
        if state["pt"] is not None:
            x, y = state["pt"]
            if 0 <= y < frame.shape[0] and 0 <= x < frame.shape[1]:
                h_, s_, v_ = (int(c) for c in hsv[y, x])
                r_, g_, b_ = (int(c) for c in frame[y, x])
                last = f"px({x},{y}) RGB=({r_},{g_},{b_}) HSV=({h_},{s_},{v_})"
                cv2.circle(base, (int(x * s), int(y * s)), 5, (255, 255, 255), 1)
                cv2.putText(base, f"HSV {h_},{s_},{v_}", (6, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
        panels = [base]
        for t in (TARGET_RED, TARGET_GREEN, TARGET_BLUE):
            mask = color_mask(frame, t, "hsv", hsv_rules, hsv)
            det, m2 = mask_stats(mask, args.min_pixels, largest=True)
            pan = cv2.resize(cv2.cvtColor((m2.astype(np.uint8) * 255), cv2.COLOR_GRAY2BGR), size,
                             interpolation=cv2.INTER_NEAREST)
            txt = f"{TARGET_NAMES[t]} px={int(mask.sum())} " + (
                f"err={det[1]:+.2f} ymax={det[3]:.2f}" if det[0] else "no detectado")
            cv2.putText(pan, txt, (6, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 1)
            panels.append(pan)
        grid = np.vstack([np.hstack(panels[:2]), np.hstack(panels[2:])])
        cv2.imshow(win, grid)
        k = cv2.waitKey(30) & 0xFF
        if k == ord("q"):
            break
        if k == ord("p"):
            cv2.imwrite("calib_snapshot.png", cv2.cvtColor(frame, cv2.COLOR_RGB2BGR))
            print("[Calibrar] guardado calib_snapshot.png;", last)
        if state["pt"] is not None and last:
            print("[Calibrar]", last)
            state["pt"] = None
    cv2.destroyAllWindows()


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--source", choices=("sim", "g1"), default="sim",
                   help="sim = simulador MuJoCo; g1 = camara del robot real (teleimager)")
    p.add_argument("--real", action="store_true",
                   help="robot real: pide confirmacion antes de start, limita velocidad, "
                        f"tope {REAL_MAX_WALK} m/s, exige imagen")
    p.add_argument("--calibrar", action="store_true", help="solo camara + mascaras de color; no publica nada")
    p.add_argument("--image-host", default=None, help="defecto: 127.0.0.1 (sim) / 192.168.123.164 (g1)")
    p.add_argument("--image-port", type=int, default=None, help="defecto: 5555 (sim) / 55555 (g1)")
    p.add_argument("--camera-key", default="ego_view", help="solo sim")
    p.add_argument("--swap-rb", action="store_true", help="si rojo y azul salen intercambiados")
    p.add_argument("--color-mode", choices=("rgb", "hsv"), default=None, help="defecto: rgb (sim) / hsv (g1)")
    p.add_argument("--hsv-file", default=None, help="JSON con rangos HSV propios")
    p.add_argument("--min-pixels", type=int, default=None, help="pixeles minimos del color (sim 60 / g1 150)")
    p.add_argument("--planner-port", type=int, default=5556, help="puerto ZMQ que lee deploy.sh (zmq_manager)")
    p.add_argument("--rate", type=float, default=50.0, help="Hz de publicacion del planner (>=20; el deploy descarta >100 ms)")
    p.add_argument("--no-start", action="store_true", help="no enviar el comando start al arrancar")
    p.add_argument("--panel-port", type=int, default=0,
                   help="puerto HTTP local para el panel web panel_g1.py (0 = apagado); con --no-gui no abre la ventana de OpenCV")
    p.add_argument("--no-gui", action="store_true", help="sin ventana (pruebas); usar con --target")
    p.add_argument("--voz", choices=("off", "g1", "pc", "udp"), default="off",
                   help="ordenes por voz: udp = microfono USB del robot via mic_stream_g1.py, pc = micro de la PC, g1 = multicast del G1 (ver voz_cajas.py)")
    p.add_argument("--voz-puerto", type=int, default=5600, help="puerto UDP para --voz udp")
    p.add_argument("--voz-ip", default=None, help="IP de la PC en 192.168.123.x (multicast del microfono del G1)")
    p.add_argument("--voz-activacion", default="zuu", help="palabra de activacion ('' = sin palabra); PARAR no la necesita")
    p.add_argument("--voz-umbral", type=float, default=300.0, help="nivel RMS minimo para detectar voz")
    p.add_argument("--voz-dispositivo", default=None, help="con --voz pc: microfono USB (indice o parte del nombre, p. ej. Insta)")
    p.add_argument("--voz-idioma", default="es-CO")
    p.add_argument("--dry-run", action="store_true",
                   help="calcula todo pero publica siempre IDLE: el robot no se mueve")
    p.add_argument("--target", type=int, default=TARGET_STOP, choices=(-1, 0, 1, 2, 3), help="objetivo inicial")
    # navegacion
    p.add_argument("--scan-ang-vel", type=float, default=None, help="rad/s al inspeccionar (sim 0.5 / real 0.3)")
    p.add_argument("--walk-speed", type=float, default=None, help="m/s (SLOW_WALK 0.1-0.8; sim 0.5 / real 0.2)")
    p.add_argument("--slow-speed", type=float, default=None, help="m/s al acercarse (sim 0.25 / real 0.15)")
    p.add_argument("--slow-y", type=float, default=0.80, help="y_max a partir del cual frena")
    p.add_argument("--stop-y", type=float, default=None,
                   help="y_max de llegada; 0.93 = ~0.85 m en el simulador. EN REAL hay que calibrarlo")
    p.add_argument("--arrive-ticks", type=int, default=5)
    p.add_argument("--alpha-err", type=float, default=0.05)
    p.add_argument("--k-turn", type=float, default=0.8)
    p.add_argument("--max-turn", type=float, default=0.6, help="rad/s")
    p.add_argument("--center-tol", type=float, default=0.35)
    p.add_argument("--lost-hold", type=float, default=0.4, help="s siguiendo recto si se pierde el color")
    args = p.parse_args()

    real = args.real
    if real and args.source != "g1":
        p.error("--real requiere --source g1")
    g1 = args.source == "g1"
    args.image_host = args.image_host or ("192.168.123.164" if g1 else "127.0.0.1")
    args.image_port = args.image_port or (55555 if g1 else 5555)
    args.color_mode = args.color_mode or ("hsv" if g1 else "rgb")
    args.min_pixels = args.min_pixels or (150 if g1 else 60)
    stop_y_given = args.stop_y is not None
    args.stop_y = args.stop_y if stop_y_given else 0.93
    args.scan_ang_vel = args.scan_ang_vel if args.scan_ang_vel is not None else (0.3 if real else 0.5)
    args.walk_speed = args.walk_speed if args.walk_speed is not None else (0.2 if real else 0.5)
    args.slow_speed = args.slow_speed if args.slow_speed is not None else (0.15 if real else 0.25)
    if real:
        if args.walk_speed > REAL_MAX_WALK:
            print(f"[Seguridad] --walk-speed limitado a {REAL_MAX_WALK} m/s")
            args.walk_speed = REAL_MAX_WALK
        args.slow_speed = min(args.slow_speed, args.walk_speed)

    hsv_rules = load_hsv_rules(args.hsv_file)

    rx = G1Receiver(args.image_host, args.image_port, args.swap_rb) if g1 else \
        SimReceiver(args.image_host, args.image_port, args.camera_key, args.swap_rb)
    rx.start()
    print(f"[Camara] fuente {args.source}: tcp://{args.image_host}:{args.image_port} | color: {args.color_mode}")

    if args.calibrar:
        calibrate(rx, args, hsv_rules)
        rx.stop = True
        return

    ctx = zmq.Context.instance()
    pub = ctx.socket(zmq.PUB)
    pub.bind(f"tcp://*:{args.planner_port}")
    print(f"[Planner] PUB en tcp://*:{args.planner_port} (cerrar antes el Pico manager: usa el mismo puerto)")
    time.sleep(0.5)  # dar tiempo a que el deploy se suscriba

    if real:
        # 1) sin imagen no se habilita nada
        t_wait = time.time()
        while rx.latest()[0] is None and time.time() - t_wait < 8.0:
            time.sleep(0.1)
        if rx.latest()[0] is None:
            print(f"[ERROR] No llegan imagenes de tcp://{args.image_host}:{args.image_port}. "
                  "Arranca teleimager-server en el PC2. No se envia start.")
            rx.stop = True
            pub.close(0)
            sys.exit(1)
        if not stop_y_given:
            print("[Nav] --stop-y = 0.93: distancia de parada calibrada en el robot real (2026-10-07, "
                  "vale para cualquier color). Cambialo con --stop-y si necesitas otra distancia.")
        print("\n" + "=" * 70)
        print(" ROBOT REAL: el robot debe estar de pie, con espacio libre y un operador")
        print(" con el mando Unitree / tecla O en la terminal del deploy.")
        print(f" walk={args.walk_speed} m/s  slow={args.slow_speed} m/s  scan={args.scan_ang_vel} rad/s")
        print("=" * 70)
        ans = input('Escribe SI (mayusculas) para enviar start y habilitar el planner: ').strip()
        if ans == "SI":
            for _ in range(3):
                pub.send(build_command(start=True, stop=False, planner=True))
                time.sleep(0.2)
            print("[Planner] comando start enviado")
        else:
            print("[Planner] sin start. Pulsa b dos veces en la ventana para enviarlo despues.")
    elif not args.no_start:
        for _ in range(3):
            pub.send(build_command(start=True, stop=False, planner=True))
            time.sleep(0.2)
        print("[Planner] comando start enviado")

    bridge = PanelBridge(args.panel_port).start() if args.panel_port else None

    voz = None
    if args.voz != "off":
        try:
            from voz_cajas import VozListener
            voz = VozListener(args.voz, args.voz_ip, args.voz_idioma, args.voz_activacion, args.voz_umbral,
                               dispositivo=args.voz_dispositivo, puerto_udp=args.voz_puerto).start()
            print(f"[Voz] activa ({args.voz}). Frases: 'zuu, busca el rojo/verde/azul', 'zuu, inspecciona', "
                  f"'zuu, quieto'; 'para'/'alto' = PARAR (sin palabra de activacion).")
        except Exception as e:  # noqa: BLE001
            print(f"[Voz] no disponible ({type(e).__name__}: {e}); sigo solo con teclado. "
                  "Instala: pip install SpeechRecognition (pc: sounddevice).")
            voz = None

    nav = Navigator(args)
    if args.target != TARGET_STOP:
        nav.set_target(args.target)

    print(__doc__)
    win = "Lo que ve el robot (head_camera)"
    dt = 1.0 / args.rate
    last_print = 0.0
    warned_noimg = False
    t_prev = time.time()
    hz = args.rate
    last_b = 0.0
    prev_label = ""
    walk_until = 0.0  # tecla w: prueba de marcha recta sin vision
    if args.dry_run:
        print("[DRY-RUN] el planner siempre recibe IDLE: el robot NO se mueve; se ve la logica en pantalla.")

    try:
        while True:
            t0 = time.time()
            frame, ft = rx.latest()
            det, mask = None, None
            if frame is not None and nav.target in RGB_RULES:
                det, mask = detect_color(frame, nav.target, args.color_mode, hsv_rules, args.min_pixels)
            if frame is None or t0 - ft > 1.0:
                if nav.target in RGB_RULES or nav.target == TARGET_SCAN:
                    # sin imagen no se camina: quieto por seguridad
                    if not warned_noimg:
                        print("[Vision] sin imagen: robot quieto "
                              "(sim: ¿run_sim_3colores.py activo? | real: ¿teleimager-server corriendo?)")
                        warned_noimg = True
                    mode, mv, fc, sp, label = MODE_IDLE, (0, 0, 0), (math.cos(nav.theta), math.sin(nav.theta), 0), -1.0, "sin imagen"
                else:
                    mode, mv, fc, sp, label = nav.step(None, t0, dt)
            else:
                warned_noimg = False
                mode, mv, fc, sp, label = nav.step(det, t0, dt)

            if label == "LLEGO" and prev_label != "LLEGO":
                print(f"[Nav] LLEGO: y_max={nav.last_ymax:.2f} >= stop-y {args.stop_y:.2f}. "
                      "Si no avanzo nada, el objeto ya estaba mas cerca que stop-y: sube --stop-y o alejalo.")
            prev_label = label

            if t0 < walk_until:  # prueba de marcha: SLOW_WALK recto, ignora la vision
                f0 = (math.cos(nav.theta), math.sin(nav.theta), 0.0)
                mode, mv, fc, sp, label = MODE_SLOW_WALK, f0, f0, float(np.clip(args.walk_speed, 0.1, 0.8)), "PRUEBA-MARCHA"

            if args.dry_run:  # prueba sin mover: solo IDLE, igual se ve toda la logica en pantalla
                pub.send(build_planner(MODE_IDLE, (0, 0, 0), (math.cos(nav.theta), math.sin(nav.theta), 0), -1.0))
            else:
                pub.send(build_planner(mode, mv, fc, sp))

            k = ""
            if frame is not None:
                view = draw_overlay(frame, nav, det, label, hz, mask, real)
            else:
                view = np.zeros((480, 640, 3), np.uint8)
                cv2.putText(view, "esperando imagen...", (30, 240),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
            if not args.no_gui:
                cv2.imshow(win, view)
                key = cv2.waitKey(1) & 0xFF
                k = chr(key).lower() if 0 < key < 128 else ""
            if bridge is not None:
                bridge.push(view)
                if not k:
                    try:
                        k = bridge.keys.get_nowait()
                    except queue.Empty:
                        pass
            if voz is not None and not k:
                if voz.src.error and not getattr(voz, "_err_shown", False):
                    voz._err_shown = True
                    print(f"[Voz] ERROR del microfono: {voz.src.error}")
                try:
                    kv = voz.cmds.get_nowait()
                    if bridge is None or bridge.voz_enabled:
                        k = kv
                    else:
                        print(f"[Voz] desactivada en el panel: se ignora la orden '{kv}'")
                except Exception:  # queue.Empty
                    pass
            if bridge is not None:
                bridge.status = {
                    "target": TARGET_NAMES[nav.target], "target_id": nav.target, "label": label,
                    "mode": int(mode), "speed": round(float(sp), 2), "hz": round(hz, 1),
                    "theta_deg": round(math.degrees(nav.theta), 1),
                    "y_max": None if det is None or not det[0] else round(float(det[3]), 3),
                    "err": None if det is None or not det[0] else round(float(det[1]), 3),
                    "stop_y": args.stop_y, "walk_speed": args.walk_speed, "real": bool(real),
                    "dry_run": bool(args.dry_run), "walking_test": bool(t0 < walk_until),
                    "voz": args.voz, "voz_enabled": bridge.voz_enabled,
                    "voz_activa": voz is not None, "frame_age": None if frame is None else round(t0 - ft, 2),
                    "t": round(t0, 2)}
            if k == "s":
                nav.set_target(TARGET_SCAN); print("[Target] inspeccionando (girando)")
            elif k == "0":
                nav.set_target(TARGET_STOP); print("[Target] quieto")
            elif k in (" ", "x"):
                walk_until = 0.0
                nav.set_target(TARGET_STOP); nav.reset_state(); print("[PARAR] planner en IDLE")
            elif k in ("1", "2", "3"):
                nav.set_target(int(k), force=True); print(f"[Target] {TARGET_NAMES[int(k)]} (reiniciado)")
            elif k == "b":
                if real and t0 - last_b > 3.0:
                    last_b = t0
                    print("[Planner] pulsa b otra vez en 3 s para confirmar el start")
                else:
                    pub.send(build_command(start=True, stop=False, planner=True)); print("[Planner] start reenviado")
                    last_b = 0.0
            elif k == "w":
                if args.dry_run:
                    print("[Prueba] --dry-run activo: el robot NO se mueve. Reinicia sin --dry-run.")
                else:
                    walk_until = time.time() + 3.0
                    print(f"[Prueba] SLOW_WALK recto 3 s a {args.walk_speed:.2f} m/s (ESPACIO para cortar)")
            elif k == "q":
                break

            now = time.time()
            if now - last_print > 1.0 and nav.target != TARGET_STOP:
                print(f"[{TARGET_NAMES[nav.target]:>14}] {label:<16} mode={mode} speed={sp:.2f} "
                      f"theta={math.degrees(nav.theta):+6.1f} det={None if det is None else tuple(round(v, 2) for v in det)}")
                last_print = now
            hz = 0.9 * hz + 0.1 / max(now - t_prev, 1e-6)
            t_prev = now
            sleep = dt - (time.time() - t0)
            if sleep > 0:
                time.sleep(sleep)
    except KeyboardInterrupt:
        pass
    finally:
        for _ in range(5):  # dejar al robot quieto
            pub.send(build_planner(MODE_IDLE, (0, 0, 0), (math.cos(nav.theta), math.sin(nav.theta), 0), -1.0))
            time.sleep(0.02)
        rx.stop = True
        if voz is not None:
            voz.close()
        if not args.no_gui:
            cv2.destroyAllWindows()
        pub.close(0)


if __name__ == "__main__":
    main()
