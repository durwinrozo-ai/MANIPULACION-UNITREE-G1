#!/usr/bin/env python3
"""Busqueda y aproximacion a cajas de colores con el G1 de 29 GDL (politica SONIC).

Equivalente de deploy_mujoco_vision.py (12 GDL / unitree_rl_gym) para el entorno
GR00T-WholeBodyControl: MuJoCo (run_sim_3colores.py) + deploy ONNX SONIC.

    run_sim_3colores.py ──imagen ZMQ :5555──▶ este programa ──planner ZMQ :5556──▶ deploy.sh (SONIC)

Este programa hace de "operador": recibe la camara head_camera del G1 simulado,
detecta rojo/verde/azul y publica mensajes `planner` (modo, movimiento, facing)
con el mismo formato que gear_sonic/scripts/pico_manager_thread_server.py.
No usa Pico 4.

Teclas (con foco en la ventana "Lo que ve el robot"; no distingue mayusculas):
    s  girar en el sitio inspeccionando        0  quieto (cancela todo)
    1  buscar y caminar a la caja roja         2  verde        3  azul
    b  reenviar el comando `start` a SONIC     q  salir
"""
import argparse
import base64
import json
import math
import struct
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
# Vision
# ----------------------------------------------------------------------------
COLOR_RULES = {
    TARGET_RED: lambda r, g, b: (r > 150) & (g < 80) & (b < 80),
    TARGET_GREEN: lambda r, g, b: (g > 150) & (r < 80) & (b < 80),
    TARGET_BLUE: lambda r, g, b: (b > 150) & (r < 80) & (g < 80),
}


def detect_color(img_rgb, rule, min_pixels=60):
    """Devuelve (detectado, error_x, y_min, y_max) o (False, 0, 0, 0).

    error_x: -1 (izquierda) .. +1 (derecha); positivo = el color esta a la derecha.
    y_max:   fila inferior del color (0 arriba .. 1 abajo). Crece al acercarse.
    """
    r = img_rgb[:, :, 0].astype(np.int32)
    g = img_rgb[:, :, 1].astype(np.int32)
    b = img_rgb[:, :, 2].astype(np.int32)
    mask = rule(r, g, b)
    if int(mask.sum()) < min_pixels:
        return False, 0.0, 0.0, 0.0
    ys, xs = np.nonzero(mask)
    h, w = img_rgb.shape[:2]
    err = (xs.mean() - w / 2.0) / (w / 2.0)
    return True, float(err), float(ys.min() / h), float(ys.max() / h)


class ImageReceiver(threading.Thread):
    """Se suscribe a la camara publicada por el simulador (msgpack + JPEG base64)."""

    def __init__(self, host, port, key, swap_rb=False):
        super().__init__(daemon=True)
        self.host, self.port, self.key, self.swap_rb = host, port, key, swap_rb
        self.lock = threading.Lock()
        self.frame = None
        self.frame_time = 0.0
        self.count = 0
        self.stop = False
        self.keys_seen = None

    def run(self):
        ctx = zmq.Context.instance()
        sock = ctx.socket(zmq.SUB)
        sock.setsockopt(zmq.CONFLATE, 1)  # solo la imagen mas reciente
        sock.setsockopt_string(zmq.SUBSCRIBE, "")
        sock.setsockopt(zmq.RCVTIMEO, 500)
        sock.connect(f"tcp://{self.host}:{self.port}")
        while not self.stop:
            try:
                raw = sock.recv()
            except zmq.Again:
                continue
            try:
                msg = msgpack.unpackb(raw, raw=False)
                images = msg["images"]
                self.keys_seen = list(images.keys())
                val = images[self.key] if self.key in images else next(iter(images.values()))
                if isinstance(val, str):
                    val = base64.b64decode(val)
                img = cv2.imdecode(np.frombuffer(val, np.uint8), cv2.IMREAD_COLOR)
                if img is None:
                    continue
                if self.swap_rb:
                    img = img[:, :, ::-1].copy()
                with self.lock:
                    self.frame = img  # RGB (ver run_camera_viewer.py)
                    self.frame_time = time.time()
                    self.count += 1
            except Exception as e:  # noqa: BLE001
                print(f"[Vision] mensaje de imagen invalido: {e}")
        sock.close(0)

    def latest(self):
        with self.lock:
            return self.frame, self.frame_time


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

    def set_target(self, t):
        if t != self.target:
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
def draw_overlay(img_rgb, nav, det, label, hz):
    out = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2BGR)
    out = cv2.resize(out, (640, 480), interpolation=cv2.INTER_NEAREST)
    txt = f"{TARGET_NAMES[nav.target]} | {label} | theta={math.degrees(nav.theta):+.0f} deg | {hz:.0f} Hz"
    cv2.putText(out, txt, (8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 2)
    if det is not None and det[0]:
        cv2.putText(out, f"err={det[1]:+.2f} y_max={det[3]:.2f}", (8, 46),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 255), 2)
    return out


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--image-host", default="127.0.0.1")
    p.add_argument("--image-port", type=int, default=5555, help="camera_port de run_sim_3colores.py")
    p.add_argument("--camera-key", default="ego_view")
    p.add_argument("--swap-rb", action="store_true", help="si rojo y azul salen intercambiados")
    p.add_argument("--planner-port", type=int, default=5556, help="puerto ZMQ que lee deploy.sh (zmq_manager)")
    p.add_argument("--rate", type=float, default=50.0, help="Hz de publicacion del planner (>=20; el deploy descarta >100 ms)")
    p.add_argument("--no-start", action="store_true", help="no enviar el comando start al arrancar")
    # navegacion
    p.add_argument("--scan-ang-vel", type=float, default=0.5, help="rad/s al inspeccionar")
    p.add_argument("--walk-speed", type=float, default=0.5, help="m/s (SLOW_WALK admite 0.1-0.8)")
    p.add_argument("--slow-speed", type=float, default=0.25, help="m/s al acercarse")
    p.add_argument("--slow-y", type=float, default=0.80, help="y_max a partir del cual frena")
    p.add_argument("--stop-y", type=float, default=0.93, help="y_max de llegada (~0.85 m de la caja)")
    p.add_argument("--arrive-ticks", type=int, default=5)
    p.add_argument("--alpha-err", type=float, default=0.05)
    p.add_argument("--k-turn", type=float, default=0.8)
    p.add_argument("--max-turn", type=float, default=0.6, help="rad/s")
    p.add_argument("--center-tol", type=float, default=0.35)
    p.add_argument("--lost-hold", type=float, default=0.4, help="s siguiendo recto si se pierde el color")
    args = p.parse_args()

    ctx = zmq.Context.instance()
    pub = ctx.socket(zmq.PUB)
    pub.bind(f"tcp://*:{args.planner_port}")
    print(f"[Planner] PUB en tcp://*:{args.planner_port} (cerrar antes el Pico manager: usa el mismo puerto)")
    time.sleep(0.5)  # dar tiempo a que el deploy se suscriba

    if not args.no_start:
        for _ in range(3):
            pub.send(build_command(start=True, stop=False, planner=True))
            time.sleep(0.2)
        print("[Planner] comando start enviado")

    rx = ImageReceiver(args.image_host, args.image_port, args.camera_key, args.swap_rb)
    rx.start()
    nav = Navigator(args)

    print(__doc__)
    win = "Lo que ve el robot (head_camera)"
    dt = 1.0 / args.rate
    last_print = 0.0
    warned_noimg = False
    t_prev = time.time()
    hz = args.rate

    try:
        while True:
            t0 = time.time()
            frame, ft = rx.latest()
            det = None
            if frame is not None and nav.target in COLOR_RULES:
                det = detect_color(frame, COLOR_RULES[nav.target])
            if frame is None or t0 - ft > 1.0:
                if nav.target in COLOR_RULES or nav.target == TARGET_SCAN:
                    # sin imagen no se camina: quieto por seguridad
                    if not warned_noimg:
                        print("[Vision] sin imagen del simulador: robot quieto "
                              "(¿run_sim_3colores.py con --enable-offscreen --enable-image-publish?)")
                        warned_noimg = True
                    mode, mv, fc, sp, label = MODE_IDLE, (0, 0, 0), (math.cos(nav.theta), math.sin(nav.theta), 0), -1.0, "sin imagen"
                else:
                    mode, mv, fc, sp, label = nav.step(None, t0, dt)
            else:
                warned_noimg = False
                mode, mv, fc, sp, label = nav.step(det, t0, dt)

            pub.send(build_planner(mode, mv, fc, sp))

            # visor + teclado (~cada tick; waitKey(1) no bloquea)
            if frame is not None:
                cv2.imshow(win, draw_overlay(frame, nav, det, label, hz))
            else:
                blank = np.zeros((480, 640, 3), np.uint8)
                cv2.putText(blank, "esperando imagen del simulador...", (30, 240),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
                cv2.imshow(win, blank)
            key = cv2.waitKey(1) & 0xFF
            k = chr(key).lower() if 0 < key < 128 else ""
            if k == "s":
                nav.set_target(TARGET_SCAN); print("[Target] inspeccionando (girando)")
            elif k == "0":
                nav.set_target(TARGET_STOP); print("[Target] quieto")
            elif k in ("1", "2", "3"):
                nav.set_target(int(k)); print(f"[Target] {TARGET_NAMES[int(k)]}")
            elif k == "b":
                pub.send(build_command(start=True, stop=False, planner=True)); print("[Planner] start reenviado")
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
    finally:
        for _ in range(5):  # dejar al robot quieto
            pub.send(build_planner(MODE_IDLE, (0, 0, 0), (math.cos(nav.theta), math.sin(nav.theta), 0), -1.0))
            time.sleep(0.02)
        rx.stop = True
        cv2.destroyAllWindows()
        pub.close(0)


if __name__ == "__main__":
    main()
