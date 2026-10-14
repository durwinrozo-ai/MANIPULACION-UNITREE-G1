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
    Gatillo derecho        INSPECCIONAR (s)
    Gatillo izquierdo      PARAR       (ESPACIO, siempre funciona)
    Clic de un joystick    QUIETO      (0)
    Grip (lateral)         SEGURO: ver --seguro

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
         ("Y", "AVANZAR", (40, 190, 230)), ("GAT.D", "INSPECC.", (200, 160, 60)),
         ("GAT.I", "PARAR", (60, 60, 200))]


# ----------------------------------------------------------------------------
# Logica de los mandos (pura: se prueba sin gafas)
# ----------------------------------------------------------------------------
class Mapper:
    """Convierte el estado de los mandos en teclas del programa de cajas.

    s: dict con rA, rB, lX, lY, rTrig, lTrig, rStick, lStick, gripL, gripR (bool).
    update() devuelve una lista de (tecla, etiqueta). Actua por flanco de subida.
    """
    KEYS = (("rA", "1", "ROJO"), ("rB", "2", "VERDE"), ("lX", "3", "AZUL"), ("rTrig", "s", "INSPECCIONAR"))

    def __init__(self, seguro="habilitar", avanzar_cada=1.0):
        self.seguro = seguro
        self.cada = avanzar_cada
        self.prev = {}
        self.last_w = -1e9
        self.avanzando = False
        self.activo = False  # hay un destino en marcha pedido desde los mandos
        self.bloqueo = ""

    def seguro_ok(self, s):
        return self.seguro == "off" or bool(s.get("gripL") or s.get("gripR"))

    def update(self, s, now):
        out = []
        self.bloqueo = ""

        def up(k):  # flanco de subida
            return bool(s.get(k)) and not self.prev.get(k)

        ok = self.seguro_ok(s)
        if up("lTrig"):
            out.append((" ", "PARAR"))
            self.avanzando = False
            self.activo = False
        for k in ("rStick", "lStick"):
            if up(k):
                out.append(("0", "QUIETO"))
                self.avanzando = False
                self.activo = False
        for name, key, label in self.KEYS:
            if up(name):
                if ok:
                    out.append((key, label))
                    self.activo = key in "123"
                    if key == "s":
                        self.activo = False
                else:
                    self.bloqueo = f"{label}: mantén un grip apretado"
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


def read_state(tv):
    g = lambda n, d=False: getattr(tv, n, d)  # noqa: E731
    return {"rA": bool(g("right_ctrl_aButton")), "rB": bool(g("right_ctrl_bButton")),
            "lX": bool(g("left_ctrl_aButton")), "lY": bool(g("left_ctrl_bButton")),
            "rTrig": bool(g("right_ctrl_trigger")), "lTrig": bool(g("left_ctrl_trigger")),
            "rStick": bool(g("right_ctrl_thumbstick")), "lStick": bool(g("left_ctrl_thumbstick")),
            "gripL": bool(g("left_ctrl_squeeze")), "gripR": bool(g("right_ctrl_squeeze"))}


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


def compose(frame, age, h, w, seguro_txt, ultimo, bloqueo, seguro_on, src):
    """Imagen final para las gafas: video + barra con la leyenda de los mandos."""
    if frame is None or age > 2.5:
        img = np.full((h, w, 3), 30, np.uint8)
        cv2.putText(img, "Sin imagen del robot", (w // 2 - 190, h // 2 - 10), cv2.FONT_HERSHEY_SIMPLEX, 1.0,
                    (255, 255, 255), 2, cv2.LINE_AA)
        cv2.putText(img, "Inicia el programa de cajas o una camara en el panel", (w // 2 - 330, h // 2 + 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (200, 200, 200), 1, cv2.LINE_AA)
    else:
        img = letterbox(frame, h, w)
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
    cv2.putText(img, f"SEGURO {'ON' if seguro_on else 'OFF'} ({seguro_txt})", (8, h - 12), cv2.FONT_HERSHEY_SIMPLEX,
                0.55, col, 1, cv2.LINE_AA)
    msg = bloqueo or (f"ultimo: {ultimo}" if ultimo else "")
    if msg:
        cv2.putText(img, msg, (w - 8 - cv2.getTextSize(msg, cv2.FONT_HERSHEY_SIMPLEX, 0.55, 1)[0][0], h - 12),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (60, 60, 230) if bloqueo else (255, 255, 255), 1, cv2.LINE_AA)
    return img


# ----------------------------------------------------------------------------
# Comunicacion con el panel
# ----------------------------------------------------------------------------
class PanelLink:
    def __init__(self, base, dry=False):
        self.base = base.rstrip("/")
        self.dry = dry
        self.q = queue.Queue()
        self.ok = None
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
                self._post(path, obj)
                self.ok = True
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
    mp = Mapper(args.seguro, args.avanzar_cada)
    print(f"[Pico] visor web listo en https://<IP de esta PC>:8012  (modo {args.modo}, seguro={args.seguro}"
          f"{', PRUEBA: no se envia nada' if args.probar else ''})")
    print("[Pico] En las gafas: https://vuer.ai?ws=wss://<IP de esta PC>:8012 -> Virtual Reality")

    ultimo, t_ultimo, bloqueo, t_bloq = "", 0.0, "", 0.0
    last_stat, last_log, was_conn = 0.0, {}, False
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
            acts = mp.update(s, t0) if conn else []
            for k, label in acts:
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
                img = compose(src.frame, age, args.alto, args.ancho, args.seguro, ultimo if t0 - t_ultimo < 4 else "",
                              bloqueo, seguro_on, src.src)
                tv.render_to_xr(img)
            if t0 - last_stat > 0.5:
                last_stat = t0
                link.status({"visor": True, "gafas": conn, "seguro": args.seguro, "seguro_on": mp.seguro_ok(s),
                             "ultimo": ultimo if t0 - t_ultimo < 6 else "", "bloqueo": bloqueo,
                             "fuente": ("programa" if ":%d" % args.vision_port in src.src else ("camara" if src.src else "")),
                             "video_ok": src.frame is not None and t0 - src.t < 2.5, "probar": args.probar,
                             "botones": [k for k, v in s.items() if v]})
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
        try:
            tv.close()
        except Exception:  # noqa: BLE001
            pass
        print("[Pico] cerrado")
    return 0


if __name__ == "__main__":
    sys.exit(main())
