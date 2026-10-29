#!/usr/bin/env python3
"""Lentes Pico 4: ver lo que ve el robot y elegir el destino con los mandos.

Corre en la PC del laboratorio, dentro del entorno conda `tv` (el de xr_teleoperate / televuer),
lo lanza el panel (boton "Lentes Pico 4") o a mano:

    conda activate tv
    python ~/tres-colores/deploy/deploy_sonic/pico_visor.py            # ver + mandos
    python ~/tres-colores/deploy/deploy_sonic/pico_visor.py --probar   # solo muestra los botones

VIDEO. Toma el MISMO video que muestra el panel: el del programa de cajas (con la deteccion y el
estado, 127.0.0.1:8765/video.mjpg) y, si ese no esta, el de la camara cruda del panel
(127.0.0.1:8080/cam.mjpg; botones RGB / Profundidad). Se lo entrega a TeleVuer (servidor WebXR
en :8012) y se agrega una barra con la leyenda de los mandos. El panel sigue mostrando todo igual.

MANDOS (controladores Pico). Mandan las MISMAS teclas que el panel (POST /api/key del panel):

    A (derecho)            ROJO        (1)
    B (derecho)            VERDE       (2)
    X (izquierdo)          AZUL        (3)
    Y (izquierdo)          AVANZAR     (w, recto; mientras se mantiene pulsado; al soltar: PARAR)
    Gatillo derecho        SELECCIONAR la caja a la que apunta el PUNTERO (ver abajo)
    Clic joystick derecho  INSPECCIONAR (s)
    Clic joystick izq.     QUIETO      (0)
    Gatillo izquierdo      PARAR       (ESPACIO, siempre funciona)
    Ambos clics a la vez   CALIBRAR el puntero (apuntando a la cruz del centro del video)
    Grip (lateral)         SEGURO: ver --seguro

PUNTERO (--puntero rayo|joystick|off). En las gafas se ve el video del robot con un recuadro fino alrededor
de cada caja detectada (rojo/verde/azul). Se apunta con el mando derecho (rayo) o se mueve el cursor con el
joystick izquierdo (--puntero joystick, alternativa que no depende de la pose del mando). La caja bajo el
cursor se resalta; el GATILLO DERECHO la elige y el robot va hacia ella (igual que pulsar su color).
Rayo: antes de usarlo, apuntar a la cruz del centro y CALIBRAR: gatillo derecho (la primera vez), o los dos
joysticks a la vez, o mantener un clic de joystick 1 s, o --cal-auto N (solo, a los N s de conectar);
--plano mundo|head dice si el plano del video esta fijo en el espacio (defecto) o pegado a la cabeza.

SEGURIDAD (--seguro):
    habilitar (defecto)  para dar un destino hay que tener un grip apretado en ese momento
    mantener             ademas, si se sueltan los dos grips el robot recibe PARAR
    off                  sin grip (no recomendado con el robot real)
PARAR (gatillo izquierdo) funciona siempre. La parada de emergencia real sigue siendo el mando
Unitree o la tecla O del deploy: estos mandos solo detienen el planner.
"""
import argparse
import json
import os
import queue
import signal
import sys
import threading
import time
import urllib.request

import cv2
import numpy as np

CHIPS = [("A", "ROJO", (60, 60, 230)), ("B", "VERDE", (80, 180, 80)), ("X", "AZUL", (230, 120, 50)),
         ("Y", "AVANZAR", (40, 190, 230)), ("GAT.D", "ELEGIR", (200, 160, 60)),
         ("GAT.I", "PARAR", (60, 60, 200))]


# ----------------------------------------------------------------------------
# Logica de los mandos (pura: se prueba sin gafas)
# ----------------------------------------------------------------------------
CAL_PANEL = 4.0  # s de cuenta atras al calibrar desde el panel (tiempo para apuntar a la cruz)


class Mapper:
    """Convierte el estado de los mandos en teclas del programa de cajas.

    s: dict con rA, rB, lX, lY, rTrig, lTrig, rStick, lStick, gripL, gripR (bool).
    punto: caja bajo el puntero (1/2/3) o None; cal: el puntero esta calibrado (o no hace falta).
    update() devuelve una lista de (tecla, etiqueta). Actua por flanco de subida.
    La tecla especial "CAL" pide calibrar el puntero (no se envia al panel).
    """
    KEYS = (("rA", "1", "ROJO"), ("rB", "2", "VERDE"), ("lX", "3", "AZUL"))
    NOMBRES = {1: "ROJO", 2: "VERDE", 3: "AZUL"}
    HOLD = 1.0  # s: un clic de joystick mantenido este tiempo calibra el puntero

    def __init__(self, seguro="habilitar", avanzar_cada=1.0, puntero=True):
        self.seguro = seguro
        self.cada = avanzar_cada
        self.puntero = puntero
        self.prev = {}
        self.last_w = -1e9
        self.avanzando = False
        self.activo = False  # hay un destino en marcha pedido desde los mandos
        self.bloqueo = ""
        self.down = {}      # instante en que se apreto cada clic de joystick
        self.anulado = {}   # ese clic ya se uso para calibrar: no actua al soltar

    def seguro_ok(self, s):
        return self.seguro == "off" or bool(s.get("gripL") or s.get("gripR"))

    def update(self, s, now, punto=None, cal=True):
        out = []
        self.bloqueo = ""

        def up(k):  # flanco de subida
            return bool(s.get(k)) and not self.prev.get(k)

        ok = self.seguro_ok(s)
        if up("lTrig"):
            out.append((" ", "PARAR"))
            self.avanzando = False
            self.activo = False
        # clics de joystick: solo (corto) = Inspeccionar / Quieto; calibrar el puntero = los dos a la vez
        # o MANTENER uno apretado HOLD segundos. El clic corto actua al soltar.
        cal_pedida = False
        for k, key, label in (("rStick", "s", "INSPECCIONAR"), ("lStick", "0", "QUIETO")):
            otro = "lStick" if k == "rStick" else "rStick"
            if up(k):
                if s.get(otro):                      # los dos a la vez
                    self.anulado.update({k: True, otro: True})
                    if not cal_pedida:
                        out.append(("CAL", "CALIBRAR puntero"))
                        cal_pedida = True
                else:
                    self.down[k] = now
                    self.anulado[k] = False
            elif s.get(k):                           # sigue apretado: calibrar si es largo
                if k in self.down and not self.anulado.get(k) and now - self.down[k] >= self.HOLD:
                    self.anulado[k] = True
                    if not cal_pedida:
                        out.append(("CAL", "CALIBRAR puntero (clic largo)"))
                        cal_pedida = True
            elif self.prev.get(k) and k in self.down:  # al soltar: clic corto
                dur = now - self.down.pop(k)
                if not self.anulado.get(k) and dur < self.HOLD:
                    out.append((key, label))  # Inspeccionar/Quieto solo giran o paran en el sitio: no piden grip
                    self.avanzando = False
                    self.activo = False
                self.anulado[k] = False
        for name, key, label in self.KEYS:
            if up(name):
                if ok:
                    out.append((key, label))
                    self.activo = True
                else:
                    self.bloqueo = f"{label}: mantén un grip apretado"
        if up("rTrig"):
            if not self.puntero:  # sin puntero: el gatillo derecho inspecciona
                out.append(("s", "INSPECCIONAR"))
            elif not cal:  # primera vez: el gatillo derecho calibra (apuntando a la cruz del centro)
                out.append(("CAL", "CALIBRAR puntero (gatillo)"))
            elif punto not in (1, 2, 3):
                self.bloqueo = "Apunta a una caja y aprieta el gatillo"
            elif ok:
                out.append((str(punto), f"{self.NOMBRES[punto]} (puntero)"))
                self.activo = True
            else:
                self.bloqueo = f"{self.NOMBRES[punto]}: mantén un grip apretado"
        # AVANZAR: mientras Y esta pulsado se repite 'w'; al soltar, PARAR
        if s.get("lY"):
            if ok:
                if now - self.last_w >= self.cada:
                    out.append(("w", "AVANZAR"))
                    self.last_w = now
                    self.avanzando = True
            elif up("lY"):
                self.bloqueo = "AVANZAR: mantén un grip apretado"
        elif self.prev.get("lY") and self.avanzando:
            out.append((" ", "PARAR (soltaste Y)"))
            self.avanzando = False
        # modo 'mantener': sin grips en pleno movimiento -> PARAR
        if self.seguro == "mantener" and not ok and (self.avanzando or self.activo):
            out.append((" ", "PARAR (soltaste el grip)"))
            self.avanzando = False
            self.activo = False
        self.prev = {k: bool(v) for k, v in s.items()}
        return out


# ----------------------------------------------------------------------------
# Puntero: rayo del mando -> pixel del video (geometria pura, se prueba sin gafas)
# ----------------------------------------------------------------------------
class Puntero:
    """Intersecta el rayo del mando con el plano donde las gafas dibujan el video.

    TeleVuer dibuja el video en un plano de alto `Hp` a `D` m (ego: 0.75 m a 2 m; inmersivo: 1 m a 1 m),
    aspecto ancho/alto de la imagen. Se asume que el plano mira al usuario y que su centro esta en
    (cx, cy, -D): `plano='mundo'` en el sistema de coordenadas del visor (fijo en el espacio) o
    `plano='head'` en el sistema de la cabeza (pegado a las gafas). El centro (cx, cy) se calibra
    apuntando al centro del video (calibrar()). El rayo sale del mando hacia su eje -z.
    """

    def __init__(self, plano="mundo", vista="ego", ancho=960, alto=540, escala=1.0, yaw=0.0, pitch=0.0):
        self.D, self.Hp = (2.0, 0.75) if vista == "ego" else (1.0, 1.0)
        self.Hp *= escala
        self.W = self.Hp * ancho / alto
        self.w, self.h = ancho, alto
        self.plano = plano
        self.cx = self.cy = None
        self.yaw, self.pitch = np.radians(yaw), np.radians(pitch)

    @property
    def calibrado(self):
        return self.cx is not None

    def _ray(self, H, M):
        o = np.array(M[:3, 3], float)
        d = -np.array(M[:3, 2], float)
        if self.yaw or self.pitch:  # ajuste fino del eje del mando
            cy_, sy_, cp, sp = np.cos(self.yaw), np.sin(self.yaw), np.cos(self.pitch), np.sin(self.pitch)
            R = np.array([[cy_, 0, sy_], [0, 1, 0], [-sy_, 0, cy_]]) @ np.array([[1, 0, 0], [0, cp, -sp], [0, sp, cp]])
            d = np.array(M[:3, :3], float) @ R @ np.array([0, 0, -1.0])
        if self.plano == "head":
            Hh = np.array(H, float)
            o = np.linalg.inv(Hh) @ np.append(o, 1.0)
            o = o[:3]
            d = Hh[:3, :3].T @ d
        return o, d

    def _hit(self, H, M):
        o, d = self._ray(H, M)
        if d[2] > -1e-6:
            return None
        t = (-self.D - o[2]) / d[2]
        if t <= 0:
            return None
        p = o + t * d
        return float(p[0]), float(p[1])

    def calibrar(self, H, M):
        h = self._hit(H, M)
        if h is None:
            return False
        self.cx, self.cy = h
        return True

    def pixel(self, H, M):
        """(u, v) en el cuadro de 960x540 que se entrega a TeleVuer, o None si no apunta al plano."""
        h = self._hit(H, M)
        if h is None or not self.calibrado:
            return None
        return ((h[0] - self.cx) / self.W + 0.5) * self.w, (0.5 - (h[1] - self.cy) / self.Hp) * self.h


class CursorJoystick:
    """Alternativa al rayo: el joystick izquierdo mueve un cursor sobre el video."""

    def __init__(self, ancho=960, alto=540, vel=0.55):
        self.w, self.h, self.vel = ancho, alto, vel
        self.u, self.v = ancho / 2.0, alto / 2.0
        self.t = None

    calibrado = True

    def update(self, stick, now):
        dt = 0.0 if self.t is None else min(0.1, now - self.t)
        self.t = now
        x, y = float(stick[0]), float(stick[1])
        if abs(x) < 0.12:
            x = 0.0
        if abs(y) < 0.12:
            y = 0.0
        self.u = float(np.clip(self.u + x * self.vel * self.w * dt, 0, self.w))
        self.v = float(np.clip(self.v + y * self.vel * self.h * dt, 0, self.h))  # y>0 = hacia abajo (eje WebXR)
        return self.u, self.v

    def pixel(self, *a):
        return self.u, self.v


def letterbox_rect(ih, iw, h, w):
    """Rectangulo (x0, y0, nw, nh) que ocupa un video de ih x iw dentro del cuadro h x w (ver letterbox())."""
    s = min(h / ih, w / iw)
    nh, nw = max(1, int(ih * s)), max(1, int(iw * s))
    return (w - nw) // 2, (h - nh) // 2, nw, nh


def caja_bajo(cajas, uv, src_hw, h, w, margen=0.03):
    """Caja (1/2/3) bajo el pixel `uv` del cuadro h x w. `cajas`: {"1": [x0,y0,x1,y1]} normalizadas al video.
    Devuelve (id, (x0,y0,x1,y1) en pixeles del cuadro) o (None, None). Con varias, la de menor area."""
    if uv is None or not cajas or src_hw is None:
        return None, None
    x0, y0, nw, nh = letterbox_rect(src_hw[0], src_hw[1], h, w)
    nx, ny = (uv[0] - x0) / nw, (uv[1] - y0) / nh
    mejor, area = None, 1e9
    for k, (bx0, by0, bx1, by1) in cajas.items():
        if bx0 - margen <= nx <= bx1 + margen and by0 - margen <= ny <= by1 + margen:
            a = (bx1 - bx0) * (by1 - by0)
            if a < area:
                mejor, area = k, a
    if mejor is None:
        return None, None
    bx0, by0, bx1, by1 = cajas[mejor]
    return int(mejor), (int(x0 + bx0 * nw), int(y0 + by0 * nh), int(x0 + bx1 * nw), int(y0 + by1 * nh))


def read_state(tv):
    g = lambda n, d=False: getattr(tv, n, d)  # noqa: E731
    return {"rA": bool(g("right_ctrl_aButton")), "rB": bool(g("right_ctrl_bButton")),
            "lX": bool(g("left_ctrl_aButton")), "lY": bool(g("left_ctrl_bButton")),
            "rTrig": bool(g("right_ctrl_trigger")), "lTrig": bool(g("left_ctrl_trigger")),
            "rStick": bool(g("right_ctrl_thumbstick")), "lStick": bool(g("left_ctrl_thumbstick")),
            "gripL": bool(g("left_ctrl_squeeze")), "gripR": bool(g("right_ctrl_squeeze"))}


def read_poses(tv, mano):
    """(cabeza 4x4, mando 4x4) o (None, None)."""
    try:
        H = np.array(tv.head_pose, float).reshape(4, 4)
        M = np.array(tv.right_arm_pose if mano == "derecha" else tv.left_arm_pose, float).reshape(4, 4)
        if not np.isfinite(H).all() or not np.isfinite(M).all() or abs(np.linalg.det(M[:3, :3])) < 1e-6 \
                or abs(np.linalg.det(H[:3, :3])) < 1e-6:
            return None, None
        return H, M
    except Exception:  # noqa: BLE001
        return None, None


# ----------------------------------------------------------------------------
# Video: MJPEG del programa de cajas (o de la camara cruda del panel)
# ----------------------------------------------------------------------------
class MjpegSource(threading.Thread):
    def __init__(self, urls):
        super().__init__(daemon=True)
        self.urls = urls
        self.frame = None
        self.t = 0.0
        self.src = ""
        self.stop = False

    def _read(self, url, max_s):
        """Lee el MJPEG de `url`. True si llego algun cuadro. Corta tras max_s (para reintentar la fuente principal)."""
        got = False
        t_ini = time.time()
        try:
            with urllib.request.urlopen(url, timeout=2.0) as r:
                buf = b""
                while not self.stop:
                    chunk = r.read(8192)
                    if not chunk:
                        break
                    buf += chunk
                    while True:
                        a = buf.find(b"\xff\xd8")
                        b = buf.find(b"\xff\xd9", a + 2) if a >= 0 else -1
                        if a < 0 or b < 0:
                            break
                        jpg, buf = buf[a:b + 2], buf[b + 2:]
                        img = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
                        if img is not None:
                            self.frame, self.t, self.src = img, time.time(), url
                            got = True
                    if len(buf) > 4_000_000:
                        buf = b""
                    if max_s and time.time() - t_ini > max_s:
                        break
        except Exception:  # noqa: BLE001
            pass
        return got

    def run(self):
        while not self.stop:
            for i, url in enumerate(self.urls):
                # la principal se lee de corrido; las de respaldo se sueltan a los 5 s para reintentar la principal
                if self._read(url, 0 if i == 0 else 5.0):
                    break
            time.sleep(0.4)


def letterbox(img, h, w):
    ih, iw = img.shape[:2]
    s = min(h / ih, w / iw)
    nh, nw = max(1, int(ih * s)), max(1, int(iw * s))
    out = np.zeros((h, w, 3), np.uint8)
    y0, x0 = (h - nh) // 2, (w - nw) // 2
    out[y0:y0 + nh, x0:x0 + nw] = cv2.resize(img, (nw, nh))
    return out


def compose(frame, age, h, w, seguro_txt, ultimo, bloqueo, seguro_on, src, cajas=None, uv=None, sel=None, cal=True,
            puntero_txt=""):
    """Imagen final para las gafas: video + recuadros de las cajas + cursor + barra con la leyenda de los mandos."""
    ok_img = frame is not None and age <= 2.5
    if not ok_img:
        img = np.full((h, w, 3), 30, np.uint8)
        cv2.putText(img, "Sin imagen del robot", (w // 2 - 190, h // 2 - 10), cv2.FONT_HERSHEY_SIMPLEX, 1.0,
                    (255, 255, 255), 2, cv2.LINE_AA)
        cv2.putText(img, "Inicia el programa de cajas o una camara en el panel", (w // 2 - 330, h // 2 + 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (200, 200, 200), 1, cv2.LINE_AA)
    else:
        img = letterbox(frame, h, w)
        # cajas seleccionables (el programa de cajas las detecta): recuadro fino; la apuntada, gruesa
        if cajas:
            x0, y0, nw, nh = letterbox_rect(frame.shape[0], frame.shape[1], h, w)
            cols = {"1": (60, 60, 255), "2": (60, 220, 60), "3": (255, 140, 50)}
            for k, (bx0, by0, bx1, by1) in cajas.items():
                p0 = (int(x0 + bx0 * nw), int(y0 + by0 * nh))
                p1 = (int(x0 + bx1 * nw), int(y0 + by1 * nh))
                es_sel = sel is not None and int(k) == sel[0]
                cv2.rectangle(img, p0, p1, cols[k], 4 if es_sel else 1)
                if es_sel:
                    cv2.putText(img, {"1": "ROJO", "2": "VERDE", "3": "AZUL"}[k] + " - gatillo derecho para ir",
                                (p0[0], max(18, p0[1] - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.6, cols[k], 2, cv2.LINE_AA)
    # cursor del puntero
    if uv is not None and ok_img:
        u, v = int(uv[0]), int(uv[1])
        if 0 <= u < w and 0 <= v < h - 64:
            col = (0, 255, 255)
            cv2.circle(img, (u, v), 12, col, 2, cv2.LINE_AA)
            cv2.line(img, (u - 20, v), (u + 20, v), col, 1, cv2.LINE_AA)
            cv2.line(img, (u, v - 20), (u, v + 20), col, 1, cv2.LINE_AA)
    if not cal and ok_img:  # cruz de calibracion en el centro
        cx, cy = w // 2, (h - 64) // 2
        cv2.line(img, (cx - 30, cy), (cx + 30, cy), (0, 255, 255), 2, cv2.LINE_AA)
        cv2.line(img, (cx, cy - 30), (cx, cy + 30), (0, 255, 255), 2, cv2.LINE_AA)
        cv2.putText(img, "CALIBRAR: apunta a la cruz y aprieta el gatillo derecho", (cx - 290, cy + 60),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2, cv2.LINE_AA)
    bar = 64
    ov = img.copy()
    cv2.rectangle(ov, (0, h - bar), (w, h), (20, 20, 20), -1)
    img = cv2.addWeighted(ov, 0.72, img, 0.28, 0)
    x = 8
    for key, label, col in CHIPS:
        txt = f"{key}:{label}"
        (tw, th), _ = cv2.getTextSize(txt, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
        cv2.rectangle(img, (x, h - bar + 6), (x + tw + 12, h - bar + 6 + 24), col, -1)
        cv2.putText(img, txt, (x + 6, h - bar + 23), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, cv2.LINE_AA)
        x += tw + 20
    col = (80, 200, 80) if seguro_on else (60, 60, 230)
    cv2.putText(img, f"SEGURO {'ON' if seguro_on else 'OFF'} ({seguro_txt})  {puntero_txt}", (8, h - 12),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, col, 1, cv2.LINE_AA)
    msg = bloqueo or (f"ultimo: {ultimo}" if ultimo else "")
    if msg:
        cv2.putText(img, msg, (w - 8 - cv2.getTextSize(msg, cv2.FONT_HERSHEY_SIMPLEX, 0.55, 1)[0][0], h - 12),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (60, 60, 230) if bloqueo else (255, 255, 255), 1, cv2.LINE_AA)
    return img


class StatusPoller(threading.Thread):
    """Lee /status del programa de cajas (cajas detectadas para el puntero)."""

    def __init__(self, url):
        super().__init__(daemon=True)
        self.url = url
        self.cajas = {}
        self.t = 0.0
        self.stop = False

    def run(self):
        while not self.stop:
            try:
                with urllib.request.urlopen(self.url, timeout=0.6) as r:
                    d = json.loads(r.read() or b"{}")
                self.cajas = {k: v for k, v in (d.get("cajas") or {}).items() if k in ("1", "2", "3")}
                self.t = time.time()
            except Exception:  # noqa: BLE001
                self.cajas = {}
            time.sleep(0.1)


# ----------------------------------------------------------------------------
# Comunicacion con el panel
# ----------------------------------------------------------------------------
class PanelLink:
    def __init__(self, base, dry=False):
        self.base = base.rstrip("/")
        self.dry = dry
        self.q = queue.Queue()
        self.ok = None
        self.cal = False  # el panel pidio calibrar el puntero
        threading.Thread(target=self._run, daemon=True).start()

    def _post(self, path, obj):
        req = urllib.request.Request(self.base + path, data=json.dumps(obj).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=1.5) as r:
            return r.read()

    def _run(self):
        while True:
            path, obj = self.q.get()
            try:
                r = self._post(path, obj)
                self.ok = True
                if path == "/api/pico_status" and json.loads(r or b"{}").get("cal"):
                    self.cal = True
            except Exception:  # noqa: BLE001
                self.ok = False

    def key(self, k):
        if not self.dry:
            self.q.put(("/api/key", {"k": k, "origen": "pico"}))

    def status(self, obj):
        if self.q.qsize() < 20:
            self.q.put(("/api/pico_status", obj))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--panel", default="http://127.0.0.1:8080", help="URL del panel (recibe las teclas)")
    ap.add_argument("--vision-port", type=int, default=8765)
    ap.add_argument("--modo", choices=["ego", "immersive"], default="ego",
                    help="ego: ventana con el video y el entorno real alrededor; immersive: todo el campo visual")
    ap.add_argument("--seguro", choices=["habilitar", "mantener", "off"], default="habilitar")
    ap.add_argument("--alto", type=int, default=540)
    ap.add_argument("--ancho", type=int, default=960)
    ap.add_argument("--fps", type=float, default=30.0)
    ap.add_argument("--avanzar-cada", type=float, default=1.0, help="s entre 'w' mientras se mantiene Y")
    ap.add_argument("--cert", default=None)
    ap.add_argument("--key", default=None)
    ap.add_argument("--puntero", choices=["rayo", "joystick", "off"], default="rayo",
                    help="elegir la caja apuntando: rayo del mando derecho, o cursor movido con el joystick izquierdo")
    ap.add_argument("--plano", choices=["mundo", "head"], default="mundo",
                    help="el plano del video esta fijo en el espacio (mundo) o pegado a las gafas (head)")
    ap.add_argument("--puntero-escala", type=float, default=1.0, help="ajusta el tamaño del plano del video si el cursor no llega a los bordes")
    ap.add_argument("--puntero-yaw", type=float, default=0.0, help="grados: ajuste fino del eje del rayo")
    ap.add_argument("--puntero-pitch", type=float, default=0.0)
    ap.add_argument("--cal-auto", type=float, default=0.0,
                    help="segundos tras conectar las gafas para calibrar solo el puntero (apuntando a la cruz); 0 = no")
    ap.add_argument("--mano", choices=["derecha", "izquierda"], default="derecha", help="mando que apunta (rayo)")
    ap.add_argument("--probar", action="store_true", help="no manda nada al robot: solo muestra los botones")
    ap.add_argument("--sin-video", action="store_true", help="solo mandos (no entrega imagen a las gafas)")
    args = ap.parse_args()
    # SIGINT/SIGTERM (boton Detener del panel) -> cierre ordenado (manda PARAR si habia un destino activo)
    signal.signal(signal.SIGINT, signal.default_int_handler)
    signal.signal(signal.SIGTERM, lambda *a: (_ for _ in ()).throw(KeyboardInterrupt()))

    try:
        from televuer import TeleVuer
    except ImportError:
        print("[Pico] ERROR: no encuentro 'televuer'. Activa el entorno conda tv (conda activate tv).")
        return 2

    # TeleVuer PRIMERO: lanza el servidor web con fork() (ver runbook del Pico).
    kw = {}
    if args.cert and args.key:
        kw = {"cert_file": args.cert, "key_file": args.key}
    tv = TeleVuer(use_hand_tracking=False, binocular=False, img_shape=(args.alto, args.ancho),
                  display_fps=args.fps, display_mode=args.modo, zmq=True, webrtc=False, **kw)
    urls = [f"http://127.0.0.1:{args.vision_port}/video.mjpg", args.panel.rstrip("/") + "/cam.mjpg"]
    src = MjpegSource(urls)
    src.start()
    link = PanelLink(args.panel, dry=args.probar)
    mp = Mapper(args.seguro, args.avanzar_cada, puntero=args.puntero != "off")
    sp = StatusPoller(f"http://127.0.0.1:{args.vision_port}/status")
    sp.start()
    if args.puntero == "rayo":
        ptr = Puntero(args.plano, args.modo, args.ancho, args.alto, args.puntero_escala, args.puntero_yaw, args.puntero_pitch)
    elif args.puntero == "joystick":
        ptr = CursorJoystick(args.ancho, args.alto)
    else:
        ptr = None
    print(f"[Pico] visor web listo en https://<IP de esta PC>:8012  (modo {args.modo}, seguro={args.seguro}"
          f"{', PRUEBA: no se envia nada' if args.probar else ''})")
    print("[Pico] En las gafas: https://<IP de esta PC>:8012/?ws=wss://<IP de esta PC>:8012 -> Virtual Reality")

    ultimo, t_ultimo, bloqueo, t_bloq = "", 0.0, "", 0.0
    uv, sel = None, None
    last_stat, last_log, was_conn = 0.0, {}, False
    t_auto = None
    t_man = None  # cuenta atras de «Calibrar puntero» pedida desde el panel
    dt = 1.0 / args.fps
    try:
        while True:
            t0 = time.time()
            conn = bool(getattr(tv, "motion_data_ready", False))
            if conn and not was_conn:
                print("[Pico] gafas conectadas: mandos activos")
            was_conn = conn
            s = read_state(tv)
            if args.probar:
                if s != last_log:
                    on = [k for k, v in s.items() if v]
                    print(f"[Pico] botones: {', '.join(on) if on else '(ninguno)'}")
                    last_log = dict(s)
            # puntero: pixel del video al que apunta el mando y caja bajo el cursor
            uv, sel, H, M = None, None, None, None
            src_hw = None if src.frame is None else src.frame.shape[:2]
            hay_cajas = bool(sp.cajas) and ":%d" % args.vision_port in src.src and t0 - sp.t < 1.5
            if ptr is not None and conn:
                if isinstance(ptr, CursorJoystick):
                    ptr.update((getattr(tv, "left_ctrl_thumbstickValue", [0, 0])), t0)
                    uv = ptr.pixel()
                else:
                    H, M = read_poses(tv, args.mano)
                    if H is not None:
                        uv = ptr.pixel(H, M)
                if hay_cajas:
                    k_sel, rect = caja_bajo(sp.cajas, uv, src_hw, args.alto, args.ancho)
                    sel = (k_sel, rect) if k_sel else None
            auto_txt = ""
            if args.cal_auto > 0 and isinstance(ptr, Puntero) and conn and not ptr.calibrado:
                if t_auto is None:
                    t_auto = t0
                falta = args.cal_auto - (t0 - t_auto)
                if falta > 0:
                    auto_txt = f"calibrando en {int(falta) + 1} s: apunta a la cruz del centro"
                elif H is not None and ptr.calibrar(H, M):
                    ultimo, t_ultimo = "puntero calibrado (automatico)", t0
                    print("[Pico] puntero calibrado (automatico)")
                    t_auto = None
            elif not conn:
                t_auto = None
            if link.cal:  # boton «Calibrar puntero» del panel: cuenta atras para apuntar a la cruz
                link.cal = False
                if isinstance(ptr, Puntero) and conn:
                    t_man = t0
                else:
                    ultimo, t_ultimo = "calibrar: sin gafas conectadas" if isinstance(ptr, Puntero) else "calibrar: el cursor con joystick no lo necesita", t0
            if t_man is not None:
                falta = CAL_PANEL - (t0 - t_man)
                if not (isinstance(ptr, Puntero) and conn):
                    t_man = None
                elif falta > 0:
                    auto_txt = f"calibrando en {int(falta) + 1} s: apunta a la cruz del centro"
                else:
                    t_man = None
                    if H is not None and ptr.calibrar(H, M):
                        ultimo, t_ultimo = "puntero calibrado (boton del panel)", t0
                    else:
                        ultimo, t_ultimo = "calibrar: sin pose del mando", t0
                    print(f"[Pico] {ultimo}")
            cal = ptr is None or ptr.calibrado
            acts = mp.update(s, t0, punto=sel[0] if sel else None, cal=cal) if conn else []
            for k, label in acts:
                if k == "CAL":
                    if isinstance(ptr, Puntero) and H is not None and ptr.calibrar(H, M):
                        label = "puntero calibrado"
                    else:
                        label = "calibrar: sin pose del mando"
                    ultimo, t_ultimo = label, t0
                    print(f"[Pico] {label}")
                    continue
                link.key(k)
                ultimo, t_ultimo = label, t0
                print(f"[Pico] {label} -> tecla {'ESPACIO' if k == ' ' else k}{' (prueba: no enviada)' if args.probar else ''}")
            if mp.bloqueo:
                bloqueo, t_bloq = mp.bloqueo, t0
            if bloqueo and t0 - t_bloq > 2.0:
                bloqueo = ""
            if not args.sin_video:
                age = t0 - src.t if src.frame is not None else 1e9
                seguro_on = mp.seguro_ok(s)
                ptxt = "" if ptr is None else (auto_txt or ("puntero: " + ("calibrado" if cal else "SIN CALIBRAR") + (
                    f" sobre {Mapper.NOMBRES[sel[0]]}" if sel else "")))
                img = compose(src.frame, age, args.alto, args.ancho, args.seguro, ultimo if t0 - t_ultimo < 4 else "",
                              bloqueo, seguro_on, src.src, cajas=sp.cajas if hay_cajas else None, uv=uv, sel=sel,
                              cal=cal, puntero_txt=ptxt)
                tv.render_to_xr(img)
            if t0 - last_stat > 0.5:
                last_stat = t0
                link.status({"visor": True, "gafas": conn, "seguro": args.seguro, "seguro_on": mp.seguro_ok(s),
                             "ultimo": ultimo if t0 - t_ultimo < 6 else "", "bloqueo": bloqueo,
                             "fuente": ("programa" if ":%d" % args.vision_port in src.src else ("camara" if src.src else "")),
                             "video_ok": src.frame is not None and t0 - src.t < 2.5, "probar": args.probar,
                             "botones": [k for k, v in s.items() if v],
                             "puntero": {"modo": args.puntero, "calibrado": bool(cal), "sobre": Mapper.NOMBRES.get(sel[0]) if sel else "",
                                         "cajas": sorted(int(k) for k in sp.cajas) if hay_cajas else []}})
            sl = dt - (time.time() - t0)
            if sl > 0:
                time.sleep(sl)
    except KeyboardInterrupt:
        pass
    finally:
        # al cerrar el visor con un destino pedido desde los mandos, no dejar al robot andando
        if not args.probar and (mp.avanzando or mp.activo):
            link.key(" ")
            time.sleep(0.3)
        src.stop = True
        sp.stop = True
        try:
            tv.close()
        except Exception:  # noqa: BLE001
            pass
        print("[Pico] cerrado")
    return 0


if __name__ == "__main__":
    sys.exit(main())
