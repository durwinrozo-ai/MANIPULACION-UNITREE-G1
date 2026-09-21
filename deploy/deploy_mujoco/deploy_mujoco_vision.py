import time
import threading

import mujoco.viewer
import mujoco
import numpy as np
from legged_gym import LEGGED_GYM_ROOT_DIR
import torch
import yaml
import cv2


def get_gravity_orientation(quaternion):
    qw = quaternion[0]
    qx = quaternion[1]
    qy = quaternion[2]
    qz = quaternion[3]

    gravity_orientation = np.zeros(3)

    gravity_orientation[0] = 2 * (-qz * qx + qw * qy)
    gravity_orientation[1] = -2 * (qz * qy + qw * qx)
    gravity_orientation[2] = 1 - 2 * (qw * qw + qz * qz)

    return gravity_orientation


def pd_control(target_q, q, kp, target_dq, dq, kd):
    return (target_q - q) * kp + (target_dq - dq) * kd


class SharedState:
    def __init__(self):
        self.lock = threading.Lock()
        self.target = -1  # -1=quieto (inicio), 0=inspeccionar/girar, 1/2/3=color
        self.r_det, self.r_err, self.r_y = False, 0.0, 0.0
        self.g_det, self.g_err, self.g_y = False, 0.0, 0.0
        self.b_det, self.b_err, self.b_y = False, 0.0, 0.0

    def update_vision(self, r, g, b):
        with self.lock:
            self.r_det, self.r_err, self.r_y = r
            self.g_det, self.g_err, self.g_y = g
            self.b_det, self.b_err, self.b_y = b

    def set_target(self, t):
        with self.lock:
            self.target = t

    def snapshot(self):
        with self.lock:
            return (self.target,
                    (self.r_det, self.r_err, self.r_y),
                    (self.g_det, self.g_err, self.g_y),
                    (self.b_det, self.b_err, self.b_y))


def detect_color(img, cond_fn):
    r = img[:, :, 0].astype(np.int32)
    g = img[:, :, 1].astype(np.int32)
    b = img[:, :, 2].astype(np.int32)
    mask = cond_fn(r, g, b)
    count = int(mask.sum())
    if count < 30:
        return False, 0.0, 0.0
    ys, xs = np.nonzero(mask)
    cx = xs.mean()
    cy = ys.mean()
    h, w = img.shape[0], img.shape[1]
    error_x = (cx - w / 2.0) / (w / 2.0)
    y_norm = cy / h
    return True, float(error_x), float(y_norm)


def vision_and_viewer_thread(model_path, shared_state, stop_flag):
    render_model = mujoco.MjModel.from_xml_path(model_path)
    render_data = mujoco.MjData(render_model)
    renderer = mujoco.Renderer(render_model, height=240, width=320)

    print("[Vision] Hilo de vision/visor iniciado.")
    print("Teclas (con foco en la ventana '(lo que ve el robot)'): 0=inspeccionar, 1=rojo, 2=verde, 3=azul, q=salir")

    while not stop_flag["stop"]:
        with shared_state.lock:
            qpos = shared_state.qpos.copy() if hasattr(shared_state, "qpos") else None
        if qpos is not None:
            render_data.qpos[:] = qpos
            mujoco.mj_forward(render_model, render_data)

            renderer.update_scene(render_data, camera="head_camera")
            img = renderer.render()

            r_res = detect_color(img, lambda r, g, b: (r > 150) & (g < 80) & (b < 80))
            g_res = detect_color(img, lambda r, g, b: (g > 150) & (r < 80) & (b < 80))
            b_res = detect_color(img, lambda r, g, b: (b > 150) & (r < 80) & (g < 80))
            shared_state.update_vision(r_res, g_res, b_res)

            img_bgr = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
            img_bgr = cv2.resize(img_bgr, (640, 480), interpolation=cv2.INTER_NEAREST)
            cv2.imshow("Lo que ve el robot (head_camera)", img_bgr)

        key = cv2.waitKey(50) & 0xFF
        if key == ord('s'):
            shared_state.set_target(0)
            print("[Target] Empezar a inspeccionar (girando)")
        elif key == ord('0'):
            shared_state.set_target(-1)
            print("[Target] Quieto")
        elif key == ord('1'):
            shared_state.set_target(1)
            print("[Target] Rojo")
        elif key == ord('2'):
            shared_state.set_target(2)
            print("[Target] Verde")
        elif key == ord('3'):
            shared_state.set_target(3)
            print("[Target] Azul")
        elif key == ord('q'):
            stop_flag["stop"] = True

    cv2.destroyAllWindows()


def compute_cmd_from_vision(shared_state, nav_state):
    target, r, g, b = shared_state.snapshot()

    if target != nav_state["last_target"]:
        nav_state["arrived"] = False
        nav_state["smoothed_error_x"] = 0.0
        nav_state["smoothed_ang_vel"] = 0.0
        nav_state["last_target"] = target

    scan_ang_vel = 0.5

    if target == -1:
        return np.array([0.0, 0.0, 0.0], dtype=np.float32)

    if target == 0:
        return np.array([0.0, 0.0, scan_ang_vel], dtype=np.float32)

    det, err, y = {1: r, 2: g, 3: b}[target]

    if nav_state["arrived"]:
        return np.array([0.0, 0.0, 0.0], dtype=np.float32)

    if not det:
        return np.array([0.0, 0.0, scan_ang_vel], dtype=np.float32)

    alpha_err = 0.05
    nav_state["smoothed_error_x"] = nav_state["smoothed_error_x"] * (1 - alpha_err) + err * alpha_err

    target_ang_vel = -0.8 * nav_state["smoothed_error_x"]
    target_ang_vel = float(np.clip(target_ang_vel, -0.6, 0.6))

    alpha_cmd = 0.1
    nav_state["smoothed_ang_vel"] = nav_state["smoothed_ang_vel"] * (1 - alpha_cmd) + target_ang_vel * alpha_cmd

    stop_y = 0.85
    if y > stop_y:
        nav_state["arrived"] = True
        return np.array([0.0, 0.0, 0.0], dtype=np.float32)

    lin_vel_x = 0.65 if abs(nav_state["smoothed_error_x"]) < 0.5 else 0.0
    return np.array([lin_vel_x, 0.0, nav_state["smoothed_ang_vel"]], dtype=np.float32)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("config_file", type=str, help="config file name in the config folder")
    args = parser.parse_args()
    config_file = args.config_file
    with open(f"{LEGGED_GYM_ROOT_DIR}/deploy/deploy_mujoco/configs/{config_file}", "r") as f:
        config = yaml.load(f, Loader=yaml.FullLoader)
        policy_path = config["policy_path"].replace("{LEGGED_GYM_ROOT_DIR}", LEGGED_GYM_ROOT_DIR)
        xml_path = config["xml_path"].replace("{LEGGED_GYM_ROOT_DIR}", LEGGED_GYM_ROOT_DIR)

        simulation_duration = config["simulation_duration"]
        simulation_dt = config["simulation_dt"]
        control_decimation = config["control_decimation"]

        kps = np.array(config["kps"], dtype=np.float32)
        kds = np.array(config["kds"], dtype=np.float32)

        default_angles = np.array(config["default_angles"], dtype=np.float32)

        ang_vel_scale = config["ang_vel_scale"]
        dof_pos_scale = config["dof_pos_scale"]
        dof_vel_scale = config["dof_vel_scale"]
        action_scale = config["action_scale"]
        cmd_scale = np.array(config["cmd_scale"], dtype=np.float32)

        num_actions = config["num_actions"]
        num_obs = config["num_obs"]

        cmd = np.array(config["cmd_init"], dtype=np.float32)

    action = np.zeros(num_actions, dtype=np.float32)
    target_dof_pos = default_angles.copy()
    obs = np.zeros(num_obs, dtype=np.float32)

    counter = 0

    m = mujoco.MjModel.from_xml_path(xml_path)
    d = mujoco.MjData(m)
    m.opt.timestep = simulation_dt

    policy = torch.jit.load(policy_path)

    shared_state = SharedState()
    shared_state.qpos = d.qpos.copy()
    stop_flag = {"stop": False}
    vthread = threading.Thread(target=vision_and_viewer_thread, args=(xml_path, shared_state, stop_flag), daemon=True)
    vthread.start()

    nav_state = {"last_target": -1, "arrived": False, "smoothed_error_x": 0.0, "smoothed_ang_vel": 0.0}

    with mujoco.viewer.launch_passive(m, d) as viewer:
        start = time.time()
        while viewer.is_running() and time.time() - start < simulation_duration and not stop_flag["stop"]:
            step_start = time.time()
            tau = pd_control(target_dof_pos, d.qpos[7:], kps, np.zeros_like(kds), d.qvel[6:], kds)
            d.ctrl[:] = tau
            mujoco.mj_step(m, d)

            with shared_state.lock:
                shared_state.qpos = d.qpos.copy()

            counter += 1
            if counter % control_decimation == 0:
                cmd = compute_cmd_from_vision(shared_state, nav_state)

                qj = d.qpos[7:]
                dqj = d.qvel[6:]
                quat = d.qpos[3:7]
                omega = d.qvel[3:6]

                qj = (qj - default_angles) * dof_pos_scale
                dqj = dqj * dof_vel_scale
                gravity_orientation = get_gravity_orientation(quat)
                omega = omega * ang_vel_scale

                period = 0.8
                count = counter * simulation_dt
                phase = count % period / period
                sin_phase = np.sin(2 * np.pi * phase)
                cos_phase = np.cos(2 * np.pi * phase)

                obs[:3] = omega
                obs[3:6] = gravity_orientation
                obs[6:9] = cmd * cmd_scale
                obs[9: 9 + num_actions] = qj
                obs[9 + num_actions: 9 + 2 * num_actions] = dqj
                obs[9 + 2 * num_actions: 9 + 3 * num_actions] = action
                obs[9 + 3 * num_actions: 9 + 3 * num_actions + 2] = np.array([sin_phase, cos_phase])
                obs_tensor = torch.from_numpy(obs).unsqueeze(0)
                action = policy(obs_tensor).detach().numpy().squeeze()
                target_dof_pos = action * action_scale + default_angles

            viewer.sync()

            time_until_next_step = m.opt.timestep - (time.time() - step_start)
            if time_until_next_step > 0:
                time.sleep(time_until_next_step)

    stop_flag["stop"] = True
