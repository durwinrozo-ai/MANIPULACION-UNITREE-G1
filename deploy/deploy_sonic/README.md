# Robot de las 3 cajas en MuJoCo con el G1 de 29 GDL (política SONIC), sin Pico 4

Migración del programa `deploy_mujoco_vision.py` (unitree_rl_gym, 12 GDL) al entorno
**GR00T-WholeBodyControl**: MuJoCo con el G1 de 29 GDL y la política SONIC (ONNX), la misma
del runbook sim2sim. Se conserva el comportamiento: quieto → `s` gira inspeccionando →
`1/2/3` busca y camina hacia rojo/verde/azul → se detiene cerca de la caja → `0` regresa a quieto.

```
Terminal 1  run_sim_3colores.py  ── imagen head_camera, ZMQ :5555 ──▶ Terminal 3
Terminal 2  deploy.sh (SONIC)    ◀── planner/command, ZMQ :5556 ───── Terminal 3
Terminal 3  deploy_sonic_vision.py  (visión + máquina de estados + teclas)
```

## Qué cambia respecto al programa de 12 GDL

| Antes (unitree_rl_gym) | Ahora (SONIC) |
|---|---|
| El script simulaba la física y aplicaba PD a 12 articulaciones | El simulador y la política son procesos aparte; este programa solo actúa de operador |
| Comando `[vx, vy, wz]` a la política | Mensaje ZMQ `planner`: `mode`, `movement` (vector mundo), `facing` (vector mundo), `speed` |
| Girar = `wz` | Girar en el sitio = modo `IDLE` con `facing` que rota (igual que el stick derecho del Pico manager) |
| Caminar = `vx` 0.65 m/s | Caminar = modo `SLOW_WALK`, `movement` = dirección del rumbo, `speed` 0.5 m/s (rango 0.1–0.8) |
| Cámara en la pelvis | Cámara `head_camera` del modelo de 29 GDL (en `torso_link`, a ~1.30 m, mira 44° hacia abajo, fovy 45°) |
| Llegada: centroide `y > 0.62` | Llegada: borde inferior del color `y_max ≥ 0.93` (≈ 0.85 m del centro de la caja) |

La detección por color, el filtro exponencial del error, el "candado" de llegada y las
coordenadas de las cajas (2.8 m, mismos ángulos) se mantienen. Las cajas no tienen colisión.

## Archivos

| Archivo | Función |
|---|---|
| `run_sim_3colores.py` | Lanza MuJoCo como `run_sim_loop.py`, agrega las cajas a la escena que ya use tu configuración y activa la publicación de la cámara |
| `escena_cajas.xml.template` | Plantilla que incluye la escena original y añade las 3 cajas (el lanzador la copia junto a la escena original) |
| `deploy_sonic_vision.py` | Recibe la cámara, detecta colores, publica `command`/`planner` a SONIC y muestra "lo que ve el robot" |

## Arranque (cada sesión, en este orden)

Sea `GR00T=/home/udirobotika/DURVVIN/SIM2SIM/blea/GR00T-WholeBodyControl` y
`REPO=` la carpeta donde clonaste este repositorio.

**Antes:** cierra el Pico manager y la app LSC si siguen abiertos (usan el puerto 5556).

**Terminal 1 — MuJoCo**

```bash
cd $GR00T
source .venv_teleop/bin/activate
python $REPO/deploy/deploy_sonic/run_sim_3colores.py
```

Debe imprimir `escena base` y `escena final`, abrir la ventana del G1 con las 3 cajas y decir
`Sensor server running at tcp://*:5555`.

**Terminal 2 — deploy SONIC**

```bash
cd $GR00T/gear_sonic_deploy
source scripts/setup_env.sh
./deploy.sh --input-type zmq_manager sim
```

Responde `y` y espera `Init Done`.

**Terminal 3 — programa de las cajas**

```bash
cd $GR00T
source .venv_teleop/bin/activate
python $REPO/deploy/deploy_sonic/deploy_sonic_vision.py
```

Envía `command start` al arrancar. En la Terminal 2 debe aparecer `Planner enabled`.
Si no aparece (el deploy aún no estaba listo), pulsa **`b`** en la ventana de la cámara.

**Soltar al robot (ventana de MuJoCo, con el foco en ella):** `7` dos veces para bajar la banda
hasta que los pies toquen el suelo y luego `9`. No pulses `9` sin bajarla antes.

**Control (foco en la ventana "Lo que ve el robot", no distingue mayúsculas):**

| Tecla | Acción |
|---|---|
| `s` | Gira en el sitio inspeccionando |
| `1` / `2` / `3` | Busca y camina hacia la caja roja / verde / azul |
| `0` | Quieto (cancela búsqueda y aproximación) |
| `b` | Reenvía `start` a SONIC |
| `q` | Sale (deja al robot quieto) |

Parada de emergencia: tecla **`O`** en la Terminal 2.

## Ajustes (argumentos de `deploy_sonic_vision.py`)

| Argumento | Defecto | Efecto |
|---|---|---|
| `--walk-speed` | 0.5 | m/s al avanzar (SLOW_WALK: 0.1–0.8) |
| `--slow-speed` / `--slow-y` | 0.25 / 0.80 | frenado suave cuando la caja ya está cerca |
| `--stop-y` | 0.93 | llegada; más bajo = se detiene más lejos |
| `--scan-ang-vel` | 0.5 | rad/s al inspeccionar |
| `--k-turn`, `--max-turn`, `--center-tol` | 0.8, 0.6, 0.35 | corrección de rumbo |
| `--alpha-err` | 0.05 | suavizado del error horizontal |
| `--swap-rb` | — | si rojo y azul salen intercambiados |
| `--image-port`, `--planner-port` | 5555, 5556 | puertos ZMQ |

## Qué se verificó y qué falta

Verificado sin el robot simulado real (entorno sin GPU ni mallas):

- La escena con cajas carga en MuJoCo con la escena por defecto de SONIC (`scene_43dof.xml`) y expone `head_camera`.
- Geometría de la cámara: mira hacia adelante y 44° hacia abajo; las cajas a 2.8 m entran por el borde superior del encuadre.
- Los mensajes `command` y `planner` son **idénticos byte a byte** a los de `tests/test_zmq_manager.py`.
- Lazo cerrado con render real de la cámara y un robot cinemático: llega a rojo (4.3 s), verde (7.4 s) y azul (13.7 s), deteniéndose entre 0.82 y 0.93 m del centro de la caja.
- El signo de la corrección de rumbo: objeto a la derecha en la imagen ⇒ girar a la derecha.

**Falta probar en tu máquina** (depende de la dinámica real de SONIC): que `IDLE` con `facing` rotando gire el robot en el sitio al ritmo esperado, y que `SLOW_WALK` a 0.5 m/s camine estable. Si camina inestable, baja `--walk-speed`; si se pasa de la caja, baja `--stop-y` o `--walk-speed`.

## Problemas frecuentes

| Síntoma | Causa | Solución |
|---|---|---|
| `esperando imagen del simulador...` | El simulador no publica la cámara | Lanzar con `run_sim_3colores.py` (activa offscreen + publicación); revisar que el puerto 5555 esté libre |
| `sin imagen del simulador: robot quieto` | Se perdió el flujo de imágenes | Reiniciar Terminal 1 |
| `Address already in use` en 5556 | Pico manager, app LSC u otra copia siguen abiertos | Cerrarlos (`fuser -k 5556/tcp`) |
| El robot no reacciona a las teclas | No llegó `start` a SONIC | Tecla `b`; el deploy debe estar en `Init Done` |
| Rojo y azul intercambiados | Orden de canales distinto | Añadir `--swap-rb` |
| Se cae al pulsar `9` | La banda lo sostenía con los pies en el aire | `7` ×2 antes de `9` |
| No detecta una caja lejana | La cámara mira 44° hacia abajo: solo ve el suelo entre ~0.6 y ~3.3 m | Acercar las cajas en `escena_cajas.xml.template` (2.8 m es el límite práctico) |
