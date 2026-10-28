#!/usr/bin/env python3
"""Lanzador de MuJoCo (G1 de 29 GDL, SONIC) con las 3 cajas de colores.

Igual que gear_sonic/scripts/run_sim_loop.py, pero:
  * toma la escena que ya usa tu configuracion (ROBOT_SCENE) y le agrega las cajas,
  * activa por defecto el render offscreen y la publicacion de la camara
    head_camera por ZMQ (puerto 5555) para deploy_sonic_vision.py.

Uso (Terminal 1, entorno .venv_teleop, desde la raiz de GR00T-WholeBodyControl):
    python <ruta>/deploy/deploy_sonic/run_sim_3colores.py
    SIM_DEPTH=1 python <ruta>/deploy/deploy_sonic/run_sim_3colores.py   # + profundidad en tcp://*:5567
    SIM_X11=x11 python ...              # abrir el visor por XWayland (ventana con barra); SIM_X11=egl: con EGL (experimental)
    SIM_WIN=800x600+100+80 python ...   # tamano/posicion de la ventana (por defecto 1000x700+60+60; SIM_WIN=off no la toca)
"""
import os
import pathlib
import sys
from typing import Dict

import tyro

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from ventana_x11 import forzar_x11

# Opcional: abrir el visor por XWayland (X11), donde la ventana tiene barra y se mueve/redimensiona.
# SIM_X11=x11 (GLX) o SIM_X11=egl; por defecto no se fuerza (en algunos equipos GLX falla). Antes de abrir el visor.
_X11 = forzar_x11()

import gear_sonic
from gear_sonic.data.robot_model.instantiation.g1 import instantiate_g1_robot_model
from gear_sonic.data.robot_model.robot_model import RobotModel
from gear_sonic.utils.mujoco_sim.configs import SimLoopConfig
from gear_sonic.utils.mujoco_sim.simulator_factory import SimulatorFactory, init_channel

HERE = pathlib.Path(__file__).resolve().parent
REPO_ROOT = pathlib.Path(gear_sonic.__file__).resolve().parent.parent  # raiz de GR00T-WholeBodyControl


class SimWrapper:
    def __init__(self, robot_model: RobotModel, env_name: str, config: Dict[str, any], **kwargs):
        self.robot_model = robot_model
        self.config = config
        init_channel(config=self.config)
        self.sim = SimulatorFactory.create_simulator(config=self.config, env_name=env_name, **kwargs)


def make_scene_with_boxes(original_scene: pathlib.Path) -> pathlib.Path:
    """Crea, junto a la escena original, un XML que la incluye y agrega las cajas."""
    tpl = (HERE / "escena_cajas.xml.template").read_text()
    out = original_scene.with_name("scene_3colores_auto.xml")
    out.write_text(tpl.replace("__ESCENA_ORIGINAL__", original_scene.name))
    return out


def main(config: SimLoopConfig):
    wbc_config = config.load_wbc_yaml()
    wbc_config["ENV_NAME"] = config.env_name

    original = pathlib.Path(wbc_config["ROBOT_SCENE"])
    if not original.is_absolute():
        original = REPO_ROOT / original
    scene = make_scene_with_boxes(original)
    wbc_config["ROBOT_SCENE"] = str(scene)  # ruta absoluta: base_sim la respeta
    print(f"[3colores] escena base : {original}")
    print(f"[3colores] escena final: {scene}")
    print(f"[3colores] ventana: {'XWayland/X11 forzado (' + _X11 + ')' if _X11 != 'off' else 'plataforma por defecto (SIM_X11=x11 o egl para forzar XWayland)'}")

    robot_model = instantiate_g1_robot_model()
    sim_wrapper = SimWrapper(
        robot_model=robot_model,
        env_name=config.env_name,
        config=wbc_config,
        onscreen=wbc_config.get("ENABLE_ONSCREEN", True),
        offscreen=wbc_config.get("ENABLE_OFFSCREEN", False),
        enable_image_publish=config.enable_image_publish,
    )
    # Ventana de MuJoCo: normal, mas pequena y movible (por defecto 1000x700). SIM_WIN=WxH+X+Y o off.
    try:
        sys.path.insert(0, str(HERE))
        from ventana_x11 import ajustar_ventana_en_hilo
        if wbc_config.get("ENABLE_ONSCREEN", True):
            ajustar_ventana_en_hilo()
    except Exception as e:  # nunca debe impedir que arranque la simulacion
        print(f"[3colores] no se pudo ajustar la ventana: {e}")
    if os.environ.get("SIM_DEPTH") == "1":  # profundidad de head_camera (ver sim_depth.py)
        sys.path.insert(0, str(HERE))
        from sim_depth import install_depth_publisher
        install_depth_publisher(sim_wrapper.sim)
    SimulatorFactory.start_simulator(
        sim_wrapper.sim,
        as_thread=False,
        enable_image_publish=config.enable_image_publish,
        mp_start_method=config.mp_start_method,
        camera_port=config.camera_port,
    )


if __name__ == "__main__":
    cfg = tyro.cli(SimLoopConfig, default=SimLoopConfig(enable_offscreen=True, enable_image_publish=True))
    main(cfg)
