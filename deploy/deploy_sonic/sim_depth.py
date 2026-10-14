"""Profundidad de la camara head_camera de MuJoCo (SONIC) publicada por ZMQ, sin tocar GR00T.

Se engancha al render que ya hace el simulador: despues de cada actualizacion del RGB renderiza la
misma camara en modo profundidad y publica un PNG de 16 bits (mm) en tcp://*:<puerto> (ver depth_cam.py).
Se activa con la variable de entorno SIM_DEPTH=1 (SIM_DEPTH_PORT=5557, SIM_DEPTH_EVERY=2, SIM_DEPTH_MAX=8.0).
"""
import os

import numpy as np


def _find_env(sim):
    """Devuelve el objeto que tiene update_render_caches (BaseSimulator.sim_env en GR00T)."""
    env = getattr(sim, "sim_env", None)
    if env is not None and hasattr(env, "update_render_caches"):
        return env
    for v in vars(sim).values():
        if hasattr(v, "update_render_caches"):
            return v
    return None


def render_depth_m(env, cam_key):
    """Renderiza la profundidad (m, float32) de la camara cam_key del entorno, o None."""
    renderer = env.renderers.get(cam_key)
    if renderer is None:
        return None
    cfg = env.camera_configs[cam_key]
    cam = cfg["params"] if "params" in cfg else cfg.get("mjcf_name", cam_key)
    renderer.enable_depth_rendering()
    try:
        renderer.update_scene(env.mj_data, camera=cam)
        d = renderer.render().copy()
    finally:
        renderer.disable_depth_rendering()
    return d


def install_depth_publisher(sim, port=None, every=None, max_m=None, cam_key="ego_view"):
    """Instala la publicacion de profundidad. Devuelve True si quedo activa."""
    import cv2
    import zmq

    port = int(port or os.environ.get("SIM_DEPTH_PORT", 5557))
    every = int(every or os.environ.get("SIM_DEPTH_EVERY", 2))
    max_m = float(max_m or os.environ.get("SIM_DEPTH_MAX", 8.0))
    env = _find_env(sim)
    if env is None or not getattr(env, "renderers", None):
        print("[sim-depth] AVISO: el simulador no tiene render offscreen/camaras; sin profundidad.")
        return False
    if cam_key not in env.renderers:
        cam_key = next(iter(env.renderers))
    pub = zmq.Context.instance().socket(zmq.PUB)
    pub.setsockopt(zmq.SNDHWM, 2)
    pub.bind(f"tcp://*:{port}")
    orig = env.update_render_caches
    state = {"n": 0}

    def patched(*a, **k):
        out = orig(*a, **k)
        state["n"] += 1
        if state["n"] % every == 0:
            try:
                d = render_depth_m(env, cam_key)
                if d is not None:
                    mm = np.where((d > 0) & (d < max_m), d * 1000.0, 0).astype(np.uint16)
                    ok, buf = cv2.imencode(".png", mm, [cv2.IMWRITE_PNG_COMPRESSION, 1])
                    if ok:
                        pub.send(buf.tobytes(), zmq.NOBLOCK)
            except Exception as e:  # noqa: BLE001  (nunca romper el simulador por la profundidad)
                if state["n"] % 300 == every:
                    print(f"[sim-depth] error al renderizar profundidad: {type(e).__name__}: {e}")
        return out

    env.update_render_caches = patched
    print(f"[sim-depth] profundidad de '{cam_key}' publicada en tcp://*:{port} (1 de cada {every} cuadros, max {max_m} m)")
    return True
