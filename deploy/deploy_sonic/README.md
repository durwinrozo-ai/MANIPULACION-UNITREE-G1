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

---

# Robot real (G1 físico, cámara RealSense de la cabeza)

`deploy_sonic_vision.py` tiene ahora `--source g1` (cámara real) y `--real` (modo seguro).
**No** se usa la Terminal 1 (MuJoCo).

```
PC2 192.168.123.164   teleimager-server --rs ── JPEG ZMQ :55555 ──▶ Terminal 3 (este programa)
PC  (5062robotika)    Terminal 2: deploy.sh --input-type zmq_manager enp131s0 ◀── planner :5556 ── Terminal 3
```

## Orden de arranque

**0. Cámara en el PC2** (`ssh unitree@192.168.123.164`). El servicio de Unitree `videohub_pc4` arranca con el robot
y ocupa la RealSense (`Device or resource busy`); un supervisor lo relanza. Se crea una vez este script en el PC2:

```bash
cat > ~/start_camera.sh <<'EOF'
#!/bin/bash
# Libera la RealSense de videohub_pc4 y arranca teleimager
sudo -v || exit 1
cd ~/teleimager
if ! grep -q 'type: realsense' cam_config_server.yaml; then
  echo "AVISO: cam_config_server.yaml no tiene head_camera como realsense; revisa el archivo."
fi
( while true; do sudo pkill -9 -x videohub_pc4; sleep 0.2; done ) 2>/dev/null &
LOOP=$!
( sleep 40; kill $LOOP 2>/dev/null ) 2>/dev/null &
trap 'kill $LOOP 2>/dev/null' EXIT
sleep 1.5
teleimager-server --rs
EOF
```

y después de **cada reinicio del robot** se ejecuta (pide la contraseña de `sudo`; debe terminar en `head_camera is ready` / `Running...`; si sale `busy`, repetir):

```bash
bash ~/start_camera.sh
```

No uses `pkill -f` con la ruta del programa: coincide con su propia línea de comandos y llena la pantalla de `Killed`.

`~/teleimager/cam_config_server.yaml` puede volver a los valores de fábrica tras un reinicio (síntoma: error de `left_wrist_camera`).
Debe quedar: `head_camera` con `enable_zmq: true`, `zmq_port: 55555`, `type: realsense`, `image_shape: [480, 640]`,
`binocular: false`, `serial_number: "347622070449"`; `left_wrist_camera` y `right_wrist_camera` con `enable_zmq: false`.
Conviene guardar una copia buena (`cp cam_config_server.yaml ~/cam_config_server.good.yaml`).

Comprobación desde el PC: `cd ~/LSC-Mafe && source venv/bin/activate && python -m robot.probar_camara_g1 192.168.123.164` → `OK: ~30 fps, frame 640x480`.

**1. Calibrar colores** (nada se envía al robot; ya calibrado, rangos en `~/tres-colores/hsv_real.json`):

```bash
cd $GR00T && source .venv_teleop/bin/activate
python $REPO/deploy/deploy_sonic/deploy_sonic_vision.py --source g1 --calibrar
```
Cuatro paneles: cámara + máscaras de rojo/verde/azul con `px`, `err` y `ymax`. Clic en la caja = HSV del píxel.
Si una caja no se detecta, ajusta rangos con `--hsv-file` (formato en la cabecera del script).

**2. Terminal 2 — deploy SONIC (robot real)**

```bash
cd $GR00T/gear_sonic_deploy
source scripts/setup_env.sh
./deploy.sh --input-type zmq_manager enp131s0
```
Responde `y` y espera `Init Done`. Con el mando Unitree o la tecla `O` a mano. No debe haber ningún `[ERROR]`.

**3. Terminal 3 — programa de las cajas** (sin `--dry-run`)

```bash
cd $GR00T && source .venv_teleop/bin/activate
python $REPO/deploy/deploy_sonic/deploy_sonic_vision.py --source g1 --real --hsv-file ~/tres-colores/hsv_real.json --walk-speed 0.3
```
Con `--real`: exige imagen (si no llega, sale sin enviar `start`), pide escribir `SI` antes de
habilitar el planner (en la Terminal 2 debe salir `Planner enabled`), limita la velocidad (defecto 0.2 m/s, tope 0.3),
gira a 0.3 rad/s y, si se pierde la imagen más de 1 s, deja el planner en IDLE.

| Tecla | Acción |
|---|---|
| `w` | Prueba de marcha: SLOW_WALK recto 3 s, sin usar la visión (aísla visión de locomoción) |
| `s` | Gira en el sitio inspeccionando |
| `1` / `2` / `3` | Busca y camina a la caja roja / verde / azul |
| ESPACIO o `x` | PARAR |
| `0` | Quieto |
| `b` | Reenvía `start` (dos veces en 3 s) |
| `q` | Salir |

## Órdenes por voz (micrófono USB Insta360 conectado al robot)

Las teclas se pueden dar hablando. El Insta360 está conectado por USB-C **al robot**, así que lo ve el computador interno del robot
(probablemente el PC2, `192.168.123.164`), no la PC. `mic_stream_g1.py` corre en el robot, captura el micrófono con `arecord` y lo
envía por UDP a la PC (PCM 16 kHz mono); `voz_cajas.py --fuente udp` lo recibe, lo segmenta en frases y lo reconoce con Google
(`SpeechRecognition`, **necesita internet** en la PC).

```
Insta360 ─USB-C─▶ robot (PC2): mic_stream_g1.py ──UDP :5600, PCM 16 kHz──▶ PC: voz_cajas.py --fuente udp ─▶ teclas de deploy_sonic_vision.py
```

El audio del G1 por multicast (`239.168.123.161:5555`) no se usa: el 08/10/2026 el robot no emitía nada por ese puerto
(solo DDS `239.255.0.1:7401` y video `230.1.1.1:1720`). `--fuente g1` queda por si se resuelve.

**Una vez, en la PC** (entorno `.venv_teleop`; no trae `pip`, se usa `uv`):
```bash
uv pip install --python "$(which python)" SpeechRecognition
```
(Si se usa un micrófono USB en la PC en lugar del del robot: `sudo apt install libportaudio2` y `uv pip install --python "$(which python)" sounddevice`.)

**Una vez, en el robot** (PC2): copiar el script y comprobar que ve el micrófono:
```bash
scp ~/tres-colores/deploy/deploy_sonic/mic_stream_g1.py unitree@192.168.123.164:~/     # desde la PC
ssh unitree@192.168.123.164
lsusb | grep -i -E "insta|arashi|2e1a"          # debe aparecer el Insta360
arecord -l                                       # debe aparecer como tarjeta de captura (USB Audio)
# si falta arecord: sudo apt install alsa-utils
python3 mic_stream_g1.py --lista
```
Si el Insta360 no aparece en `lsusb`/`arecord -l`: cambiar el modo USB de la cámara (webcam/micrófono, no almacenamiento) y revisar el cable.
Si está conectado a otro de los computadores del robot, probar `ssh unitree@192.168.123.161` (PC1) con los mismos comandos.

**1. Probar la voz SIN el programa de las cajas.**

En la PC (Terminal V1), primero el receptor:
```bash
cd ~/tres-colores/deploy/deploy_sonic
python voz_cajas.py --fuente udp
```
En el robot (Terminal V2), después el emisor (IP de la PC en el cable, `192.168.123.222`):
```bash
python3 mic_stream_g1.py --nombre Insta --destino 192.168.123.222        # o --tarjeta N con el número de arecord -l
```
Habla cerca del micrófono: en la PC `paquetes` debe subir, `nivel` superar `umbral` y salir `[Voz] oi: "..." -> comando ...`.
Si `paquetes=0`: firewall UDP 5600 de la PC (`sudo ufw status`) o IP de destino equivocada. Si el nivel es muy bajo o detecta ruido, ajustar `--umbral`
(defecto 300; el ruido de fondo se mide solo y el umbral nunca baja de 3× ese ruido).

**2. Usarla con el programa de las cajas** (emisor corriendo en el robot; en la Terminal 3 se añade `--voz udp`):
```bash
python ~/tres-colores/deploy/deploy_sonic/deploy_sonic_vision.py --source g1 --real --hsv-file ~/tres-colores/hsv_real.json --walk-speed 0.3 --voz udp
```
(El emisor en el robot es otra terminal más; hay que dejarla abierta. Con `--voz pc --voz-dispositivo Insta` se usaría un micrófono USB de la PC; `--voz-puerto` cambia el 5600.)

| Di | Equivale a |
|---|---|
| «zuu, busca el rojo» / «zuu, ve al verde» / «zuu, azul» | `1` / `2` / `3` |
| «zuu, inspecciona» / «zuu, gira» | `s` |
| «zuu, quieto» | `0` |
| «para» / «alto» / «detente» / «stop» | **ESPACIO (PARAR)**, sin palabra de activación |

- Las órdenes de movimiento exigen la **palabra de activación** «zuu» (se aceptan variantes que Google suele escribir: zu, su, zoo, suu...).
  `--voz-activacion ''` la quita. PARAR no la necesita.
- Frases con dos colores («rojo y azul») o no reconocidas se ignoran y se imprime `[Voz] oi: "..." -> ignorado (motivo)`.
- No hay orden de voz para `w` ni `q`: la prueba de marcha y la salida siguen siendo solo por teclado.
- El teclado sigue funcionando a la vez. **No confiar en la voz para detener al robot** (el reconocimiento puede fallar o tardar 1–2 s):
  la parada real es la tecla `O` en la Terminal 2 o el mando Unitree.
- El micrófono está en el robot: el ruido de sus motores entra en el audio y la voz llega más baja a distancia. Hablar cerca del robot.

**Estado:** probado sin hardware (interpretación de frases, segmentación del audio, emisor + receptor UDP de extremo a extremo con un `arecord` simulado,
remuestreo, selección de dispositivo). **Falta:** probar con el Insta360 real conectado al robot, la precisión del reconocimiento de «zuu», el ruido de los motores y la latencia.

## Distancia de parada (calibrada en el robot real, 07/10/2026)

`--stop-y` = **0.93** (por defecto, vale para rojo, verde y azul). `y_max` es el borde inferior del objeto en la imagen,
así que depende de la distancia al suelo y no del color. Velocidades: avance 0.3 m/s (defecto real 0.2, tope 0.3);
desde `y_max > 0.80` (`--slow-y`) baja a 0.15 m/s. Con la caja a ~1.5 m, `y_max` ≈ 0.14. La línea de parada queda a ~7 % del borde
inferior de la imagen: si el robot se pasa por inercia la caja sale del encuadre; en ese caso baja `--walk-speed` o `--slow-y`.
Pendiente anotar la distancia real (cinta) en esa posición.

## Escalera de pruebas

1. Solo ver: `--calibrar`, comprobar las máscaras con las cajas reales.
2. Tecla `w` con el robot de pie, el pie en el suelo y un operador al lado.
3. Giro en el sitio (`s`).
4. Caja a ~1.5 m (si está más cerca que la parada dirá `LLEGO` sin caminar).
5. Aumentar distancia/velocidad poco a poco (máx. 0.3 m/s mientras no esté validado).

## Qué se verificó y qué falta

Verificado con un emisor ZMQ falso: lectura de JPEG en 4 formatos de mensaje (crudo, con cabecera,
multiparte, estéreo → mitad izquierda), detección HSV de los 3 colores con 3 iluminaciones y sin falsos
positivos cruzados, `start` solo tras `SI`, aborto sin imagen, IDLE al perder la imagen, tope de velocidad.
Verificado en el robot real: cámara (30,7 fps), detección de los 3 colores, `Planner enabled`, el deploy procesa el `facing` del giro de inspección,
distancia de parada.
**Falta en el robot:** confirmar la marcha `SLOW_WALK` (nunca se ha visto caminar al robot con este programa).

## Diagnóstico: "LLEGO" sin avanzar / el robot no se mueve

- **Terminal 2 muestra `[ERROR] Lost LowState data connection` / `Safety check failed`:** el deploy se apagó por perder la
  comunicación con el robot (cable, interfaz `enp131s0`). La Terminal 3 sigue diciendo «avanzando» pero nadie recibe las órdenes.
  Revisar `ip -br addr show enp131s0`, `ping 192.168.123.161`, `ip route get 192.168.123.164`, y relanzar la Terminal 2.
- **Mensaje `[Nav] LLEGO: y_max=X >= stop-y Y`:** el objeto ya estaba más cerca que la parada (0.93). Alejarlo.
- **Prueba clave:** pulsar `w`. La Terminal 3 imprime `[Prueba] SLOW_WALK recto 3 s...` y la Terminal 2 debe mostrar
  `Replanning with mode: SLOW_WALK` con `movement` distinto de cero. Si no aparece: la orden no llega (¿script viejo sin `w`? comprobar con
  `grep -c walk_until deploy_sonic_vision.py` = 4; ¿`--dry-run`?). Si aparece y el robot no se mueve: problema físico o de la política
  (pies en el suelo, control activo, probar `--walk-speed 0.3`).
- `--dry-run`: calcula y muestra todo pero el planner recibe siempre IDLE; el robot no se mueve (al arrancar imprime `[DRY-RUN]`).
- Pulsar `1/2/3` reinicia la llegada aunque el objetivo sea el mismo.
- En la Terminal 2, si el log es solo `mode: IDLE ... movement: [0, 0, 0]`, el deploy está sano pero nunca recibió una orden de marcha.
- Orden si el robot no camina: (1) `Planner enabled`; (2) robot rígido/equilibrando; (3) etiqueta `avanzando mode=1` en la Terminal 3; (4) `SLOW_WALK` en la Terminal 2.

## Cierre de una prueba

1. ESPACIO (robot quieto) → 2. `q` en la Terminal 3 → 3. Ctrl+C en la Terminal 2 → 4. Ctrl+C en la Terminal A (opcional) → 5. robot sentado o en arnés.
`fuser 5556/tcp` no debe devolver procesos antes de relanzar (si los hay: `fuser -k 5556/tcp`).
