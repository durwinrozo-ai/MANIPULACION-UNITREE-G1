#!/usr/bin/env python3
"""Profundidad del G1 (RealSense D435i real o camara de MuJoCo) en el PC: recepcion y visualizacion.

Formato de red (igual en el robot real y en el simulador): cada mensaje ZMQ PUB es UN PNG de 16 bits
(cv2.imencode('.png', uint16)) con la profundidad en MILIMETROS, alineada a la imagen a color (0 = sin dato).
  * robot real : realsense_pub.py en el PC2  -> tcp://192.168.123.164:55557  (RGB JPEG en :55555)
  * simulador  : run_sim_3colores.py con SIM_DEPTH=1 -> tcp://127.0.0.1:5557 (RGB en :5555)

Uso suelto (ver la profundidad en una ventana):
    python depth_cam.py --host 192.168.123.164 --port 55557
"""
import argparse
import threading
import time

import cv2
import numpy as np
import zmq

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def encode_depth_mm(depth_mm):
    """uint16 (mm) -> bytes PNG (compresion baja para que sea rapido)."""
    ok, buf = cv2.imencode(".png", depth_mm.astype(np.uint16), [cv2.IMWRITE_PNG_COMPRESSION, 1])
    if not ok:
        raise RuntimeError("no se pudo codificar la profundidad")
    return buf.tobytes()


def decode_depth(parts):
    """Mensaje ZMQ (una o varias partes) -> uint16 mm (HxW) o None."""
    for p in sorted(parts, key=len, reverse=True):
        i = p.find(PNG_MAGIC)
        if i < 0:
            continue
        img = cv2.imdecode(np.frombuffer(p[i:], np.uint8), cv2.IMREAD_UNCHANGED)
        if img is not None and img.dtype == np.uint16 and img.ndim == 2:
            return img
    return None


class DepthReceiver(threading.Thread):
    def __init__(self, host, port):
        super().__init__(daemon=True)
        self.host, self.port = host, port
        self.lock = threading.Lock()
        self.depth = None
        self.depth_time = 0.0
        self.count = 0
        self.stop = False

    def run(self):
        sock = zmq.Context.instance().socket(zmq.SUB)
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
                d = decode_depth(parts)
            except Exception:  # noqa: BLE001
                d = None
            if d is not None:
                with self.lock:
                    self.depth, self.depth_time, self.count = d, time.time(), self.count + 1
        sock.close(0)

    def latest(self):
        with self.lock:
            return self.depth, self.depth_time


def center_distance_m(depth_mm, k=9):
    """Mediana de los pixeles validos en un cuadrado k x k en el centro (m) o None."""
    h, w = depth_mm.shape
    r = depth_mm[h // 2 - k // 2: h // 2 + k // 2 + 1, w // 2 - k // 2: w // 2 + k // 2 + 1]
    v = r[r > 0]
    return None if v.size < max(3, r.size // 6) else float(np.median(v)) / 1000.0


def colorize(depth_mm, near=0.2, far=4.0, size=(640, 480)):
    """Profundidad -> imagen BGR con mapa de color (cerca = calido, lejos = frio, sin dato = negro),
    mira central con la distancia y barra de escala."""
    valid = depth_mm > 0
    z = depth_mm.astype(np.float32) / 1000.0
    norm = np.clip((z - near) / max(far - near, 1e-3), 0, 1)
    img = cv2.applyColorMap(((1.0 - norm) * 255).astype(np.uint8), cv2.COLORMAP_TURBO)
    img[~valid] = 0
    img = cv2.resize(img, size, interpolation=cv2.INTER_NEAREST)
    H, W = img.shape[:2]
    # barra de escala a la derecha
    bar = cv2.applyColorMap(np.linspace(255, 0, H).astype(np.uint8).reshape(H, 1), cv2.COLORMAP_TURBO)
    bar = cv2.resize(bar, (14, H), interpolation=cv2.INTER_NEAREST)
    img[:, W - 14:] = bar
    for frac in (0.0, 0.5, 1.0):
        y = int(frac * (H - 14)) + 12
        cv2.putText(img, f"{near + frac * (far - near):.1f}m", (W - 62, y), cv2.FONT_HERSHEY_SIMPLEX, 0.4,
                    (255, 255, 255), 1)
    d = center_distance_m(depth_mm)
    cx, cy = W // 2, H // 2
    cv2.drawMarker(img, (cx, cy), (255, 255, 255), cv2.MARKER_CROSS, 22, 2)
    txt = f"centro: {d:.2f} m" if d is not None else "centro: sin dato"
    cv2.putText(img, "PROFUNDIDAD  " + txt, (8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
    return img


def compose(rgb_rgb, depth_mm, mode, near=0.2, far=4.0):
    """Imagen BGR a mostrar segun el modo: 'rgb' | 'depth' | 'both'. Devuelve (bgr, distancia_centro_m)."""
    size = (640, 480)
    dist = None if depth_mm is None else center_distance_m(depth_mm)

    def blank(msg):
        b = np.zeros((480, 640, 3), np.uint8)
        cv2.putText(b, msg, (24, 240), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (200, 200, 200), 2)
        return b

    rgb = None
    if rgb_rgb is not None:
        rgb = cv2.resize(cv2.cvtColor(rgb_rgb, cv2.COLOR_RGB2BGR), size)
        cv2.putText(rgb, "RGB", (8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
    dep = colorize(depth_mm, near, far, size) if depth_mm is not None else None
    if mode == "rgb":
        return (rgb if rgb is not None else blank("esperando imagen RGB...")), dist
    if mode == "depth":
        return (dep if dep is not None else blank("esperando profundidad...")), dist
    a = rgb if rgb is not None else blank("esperando imagen RGB...")
    b = dep if dep is not None else blank("esperando profundidad...")
    return np.hstack([a, b]), dist


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="192.168.123.164")
    ap.add_argument("--port", type=int, default=55557)
    ap.add_argument("--far", type=float, default=4.0, help="distancia (m) del extremo frio de la escala")
    a = ap.parse_args()
    rx = DepthReceiver(a.host, a.port)
    rx.start()
    print(f"[Profundidad] esperando PNG16 de tcp://{a.host}:{a.port} (q para salir)")
    try:
        while True:
            d, t = rx.latest()
            if d is not None:
                cv2.imshow("Profundidad", colorize(d, far=a.far))
            if (cv2.waitKey(30) & 0xFF) == ord("q"):
                break
    except KeyboardInterrupt:
        pass
    rx.stop = True
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
