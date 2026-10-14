#!/usr/bin/env python3
"""Publica la RealSense D435i del G1 con RGB y PROFUNDIDAD (corre en el PC2 del robot, python3.8).

teleimager-server solo publica el color; esta alternativa abre UNA sola vez la camara (la RealSense no
admite dos procesos) y publica:
    tcp://*:55555   JPEG del color (mismo formato que teleimager: lo lee deploy_sonic_vision.py sin cambios)
    tcp://*:55557   PNG de 16 bits con la profundidad en mm, alineada al color (0 = sin dato)

Uso (lo lanza start_camera.sh rgbd, que antes libera la camara de videohub_pc4):
    python3 ~/realsense_pub.py [--serial 347622070449] [--ancho 640 --alto 480 --fps 30]
"""
import argparse
import signal
import sys
import time

import cv2
import numpy as np
import pyrealsense2 as rs
import zmq


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", default="347622070449")
    ap.add_argument("--ancho", type=int, default=640)
    ap.add_argument("--alto", type=int, default=480)
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--puerto-rgb", type=int, default=55555)
    ap.add_argument("--puerto-depth", type=int, default=55557)
    ap.add_argument("--calidad", type=int, default=85, help="calidad JPEG del color")
    ap.add_argument("--depth-cada", type=int, default=2, help="publica 1 de cada N cuadros de profundidad (2 = 15 Hz)")
    ap.add_argument("--solo-rgb", action="store_true", help="no publica profundidad")
    a = ap.parse_args()

    ctx = zmq.Context()
    pub_rgb = ctx.socket(zmq.PUB)
    pub_rgb.setsockopt(zmq.SNDHWM, 2)
    pub_rgb.bind(f"tcp://*:{a.puerto_rgb}")
    pub_d = None
    if not a.solo_rgb:
        pub_d = ctx.socket(zmq.PUB)
        pub_d.setsockopt(zmq.SNDHWM, 2)
        pub_d.bind(f"tcp://*:{a.puerto_depth}")

    cfg = rs.config()
    if a.serial:
        cfg.enable_device(a.serial)
    cfg.enable_stream(rs.stream.color, a.ancho, a.alto, rs.format.bgr8, a.fps)
    if pub_d is not None:
        cfg.enable_stream(rs.stream.depth, a.ancho, a.alto, rs.format.z16, a.fps)
    pipe = rs.pipeline()
    try:
        prof = pipe.start(cfg)
    except RuntimeError as e:
        print(f"[rs] ERROR al abrir la RealSense: {e}. ¿Otro proceso la usa (videohub_pc4/teleimager)? Usa start_camera.sh.")
        sys.exit(1)
    scale = 0.001
    align = None
    if pub_d is not None:
        scale = prof.get_device().first_depth_sensor().get_depth_scale()
        align = rs.align(rs.stream.color)
    print(f"[rs] camara abierta {a.ancho}x{a.alto}@{a.fps}, escala de profundidad {scale} m/unidad")

    run = {"on": True}
    signal.signal(signal.SIGINT, lambda *_: run.update(on=False))
    signal.signal(signal.SIGTERM, lambda *_: run.update(on=False))

    n = n_rgb = n_d = 0
    t_rep = time.time()
    while run["on"]:
        try:
            frames = pipe.wait_for_frames(2000)
        except RuntimeError:
            print("[rs] sin cuadros en 2 s (¿camara desconectada?)")
            continue
        if align is not None:
            frames = align.process(frames)
        c = frames.get_color_frame()
        if c:
            ok, buf = cv2.imencode(".jpg", np.asanyarray(c.get_data()), [cv2.IMWRITE_JPEG_QUALITY, a.calidad])
            if ok:
                pub_rgb.send(buf.tobytes())
                n_rgb += 1
        n += 1
        if pub_d is not None and n % max(1, a.depth_cada) == 0:
            d = frames.get_depth_frame()
            if d:
                mm = (np.asanyarray(d.get_data()).astype(np.float32) * scale * 1000.0).clip(0, 65535).astype(np.uint16)
                ok, buf = cv2.imencode(".png", mm, [cv2.IMWRITE_PNG_COMPRESSION, 1])
                if ok:
                    pub_d.send(buf.tobytes())
                    n_d += 1
        if time.time() - t_rep > 5:
            dt = time.time() - t_rep
            print(f"[rs] publicando RGB :{a.puerto_rgb} ({n_rgb / dt:.1f} fps)"
                  + (f" y profundidad :{a.puerto_depth} ({n_d / dt:.1f} fps)" if pub_d is not None else ""), flush=True)
            n_rgb = n_d = 0
            t_rep = time.time()
    pipe.stop()
    pub_rgb.close(0)
    if pub_d is not None:
        pub_d.close(0)
    print("[rs] detenido")


if __name__ == "__main__":
    main()
