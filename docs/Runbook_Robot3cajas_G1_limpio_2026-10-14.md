# Runbook · Robot 3 cajas con el Unitree G1 (SONIC)

**Versión limpia · 14/10/2026** · Robot real (G1 de 29 GDL) y simulador MuJoCo · se opera con el panel web.

El robot inspecciona, busca la caja roja, verde o azul, camina hacia ella y se detiene cuando su borde inferior llega a `y_max = 0.93` (igual para los tres colores). Se maneja con botones del panel, con teclado o por voz («zuu, busca el rojo»).

## 1. Qué hay y dónde corre

| Pieza | Dónde | Función |
|---|---|---|
| `panel_g1.py` + `panel_g1.html` | PC del laboratorio (5062robotika, usuario `udirobotika`) | Panel web `http://127.0.0.1:8080`: arranca y detiene todo, muestra cámaras, botones, voz |
| `deploy_sonic_vision.py` | PC | Detecta colores, decide el movimiento y lo envía a SONIC por ZMQ `:5556` |
| `deploy.sh` (SONIC) | PC | Política de equilibrio y marcha; habla con el robot por `enp131s0` (o con el simulador) |
| `robot/start_camera.sh`, `robot/realsense_pub.py` | PC2 del robot (`192.168.123.164`) | Cámara RealSense: RGB (`:55555`) y, opcional, profundidad (`:55557`) |
| `mic_stream_g1.py`, `voz_cajas.py` | Robot → PC (UDP `:5600`) | Voz con el micrófono Insta360 del robot |
| `run_sim_3colores.py`, `sim_depth.py` | PC | Simulador MuJoCo con las 3 cajas y cámara (RGB `:5555`, profundidad `:5567`) |
| `depth_cam.py` | PC | Receptor/visor de profundidad (PNG 16 bits en mm, 0 = sin dato) |

Red: PC1 `192.168.123.161`, PC2 `192.168.123.164`, PC del laboratorio `192.168.123.222` (`enp131s0`).

## 2. Antes de empezar

1. Cierra las terminales manuales viejas (deploy, cámara, simulador): ocupan el puerto 5556 y la cámara.
2. Robot real: encendido, **de pie**, cable Ethernet conectado, espacio libre, control Unitree (o tecla `O`) a mano.
3. Revisa la red en la PC:

| Comprobación | Debe verse |
|---|---|
| `ip -br addr show enp131s0` | `UP` y `192.168.123.222/24` |
| `ping -c 3 192.168.123.161` y `ping -c 3 192.168.123.164` | respuesta |
| `ip route get 192.168.123.164` | `dev enp131s0` |

Si falla: revisa cable y que el robot haya terminado de arrancar (30–60 s). Sin IP: `sudo ip addr add 192.168.123.222/24 dev enp131s0 && sudo ip link set enp131s0 up`.

## 3. Traer la última versión y abrir el panel

```bash
cd ~/tres-colores && git pull          # si pide, antes: git stash
cd /home/udirobotika/DURVVIN/SIM2SIM/blea/GR00T-WholeBodyControl
source .venv_teleop/bin/activate
python ~/tres-colores/deploy/deploy_sonic/panel_g1.py
```

Abre `http://127.0.0.1:8080` y deja esa terminal abierta. La contraseña del robot y los cambios de código se aplican al reiniciar el panel (Ctrl+C y volver a lanzar).

## 4. Robot real, paso a paso

1. Modo **Robot real**.
2. Escribe la contraseña del robot y **Guardar en memoria** (vive solo en memoria; repetir tras cada reinicio del panel).
3. Primera vez o tras actualizar: **Preparar robot (scp)** (copia `mic_stream_g1.py`, `start_camera.sh`, `realsense_pub.py`).
4. «Servidor de cámara del robot»: **teleimager** (solo RGB, validado) o **RealSense propio** (RGB + profundidad; reinicia la cámara A).
5. Comprueba PC1, PC2 e Interfaz en verde y marca la casilla de seguridad.
6. **Arrancar todo**: cámara (A) → micrófono (M) → deploy (2) → programa de cajas (3). Confirma `SI` cuando lo pida; en el registro del deploy debe verse `Planner enabled`.
7. Prueba primero la marcha con **Prueba marcha** (`w`, 3 s), luego **Inspeccionar** (`s`) y **Rojo / Verde / Azul** (`1/2/3`).
8. Para detener: **PARAR** (ESPACIO) o **Detener todo**. Emergencia: **EMERGENCIA** del panel, tecla `O` en el deploy o control Unitree.

## 5. Simulador MuJoCo

1. Modo **Simulador MuJoCo**; marca «Simulador con profundidad» si quieres esa cámara.
2. **Arrancar todo**: simulador (1) → deploy (2) → programa de cajas (3). El simulador siempre va antes que el deploy.
3. En la ventana de MuJoCo suelta el robot: `7` dos veces y luego `9`.
4. Usa los mismos botones que en el robot real.

## 6. Cámaras RGB y profundidad

La fila **Cámara** del panel tiene **RGB**, **Profundidad**, **RGB + profundidad** y **Ocultar**. La profundidad se ve en mapa de color con la distancia (m) al centro; el alcance se cambia en Ajustes (4 m por defecto).

| | Robot real | Simulador |
|---|---|---|
| RGB | `head_camera` (`:55555`) | `head_camera` (`:5555`) |
| Profundidad | `realsense_pub.py` en el PC2 (`:55557`), opción «RealSense propio» | Casilla «Simulador con profundidad» (`:5567`), reiniciar el simulador |

Visor suelto: `python depth_cam.py --host 192.168.123.164 --port 55557`.

## 7. Marcha y velocidad

- Solo se usan dos modos de SONIC: `IDLE` (quieto o giro en sitio, 0.3 rad/s) y `SLOW_WALK` (0.1–0.8 m/s).
- **Marcha continua:** con la caja poco desviada el robot camina sin parar y corrige el rumbo. Solo se detiene a girar en sitio si la desviación supera `--turn-tol` (0.65) y vuelve a caminar cuando baja de `--center-tol` (0.2). Si aún hay paradas, sube `--turn-tol` a 0.8.
- Valores por defecto: avance 0.3 m/s (tope real 0.3), acercamiento 0.15 m/s desde `y_max > 0.80`, parada en `y_max ≥ 0.93`.
- Subir velocidad por pasos (Ajustes → tope de velocidad real, o `--max-walk` hasta 0.8), probando cada paso con `w` y luego con la caja:

| Paso | Avance (m/s) | `--slow-y` | `--slow-speed` |
|---|---|---|---|
| 0 (actual) | 0.3 | 0.80 | 0.15 |
| 1 | 0.4 | 0.75 | 0.20 |
| 2 | 0.5 | 0.70 | 0.20 |
| 3 | 0.6 o más | 0.65 | 0.25 |

## 8. Voz

El micrófono Insta360 va por USB-C al robot: `mic_stream_g1.py` lo envía por UDP `:5600` a la PC y `voz_cajas.py` lo reconoce (Google, **requiere internet**). En el panel: activar micrófono (M) y «Aceptar órdenes de voz». Una vez en la PC: `uv pip install --python "$(which python)" SpeechRecognition`.

Frases: «zuu, busca el rojo / verde / azul», «inspecciona», «quieto». **«Para» o «alto» detienen el planner** (no sustituyen la parada de emergencia).

## 9. Cerrar una prueba

1. **PARAR** (ESPACIO): robot quieto.
2. **Detener todo** en el panel (cierra programa de cajas, deploy, micrófono y cámara en orden).
3. Robot sentado o en arnés.
4. Antes de relanzar: `fuser 5556/tcp` vacío (si no, botón **Liberar puerto 5556**).

## 10. Problemas frecuentes

| Síntoma | Solución |
|---|---|
| PC1, PC2 o Interfaz en rojo; `No route to host` | Revisar la red (sección 2) |
| El registro de A/M se queda sin avanzar | Guardar la contraseña del robot en el panel |
| `Device or resource busy` en la cámara | «¿Quién usa la cámara?»; `start_camera.sh` ya libera la cámara |
| `Address already in use` en 5556 | **Liberar puerto 5556** |
| Deploy falla con `Address already in use` en 5557 | Resuelto: la profundidad del simulador ahora usa 5567. Si tienes `panel_config.json` viejo, pon 5567 en Ajustes |
| Simulador marcado ERROR pero abierto | Resuelto en la última versión (el aviso «Channel factory init error» es inofensivo): `git pull` y reiniciar el panel |
| `Lost LowState` en el deploy | Se perdió la red con el robot: revisar sección 2 y reiniciar el deploy |
| «No llega profundidad» | Real: elegir «RealSense propio» y reiniciar A. Simulador: marcar la casilla y reiniciar el simulador |
| Dice `LLEGO` sin caminar | La caja ya está más cerca que la parada; alejarla |
| El robot no camina | Ver `Planner enabled`, `avanzando mode=1` en el programa de cajas y `SLOW_WALK` en el deploy |

## 11. Estado de la validación

**Verificado:** cámara RGB real (30 fps), detección de los 3 colores, panel completo con procesos simulados, contraseña/ssh, simulador con el panel, `Planner enabled`, micrófono → UDP → reconocimiento (prueba aislada), pipeline de profundidad con RealSense simulada.

**Pendiente en el robot:** confirmar la marcha continua tras el ajuste de histéresis, llegar a la caja y medir con cinta la distancia real de parada, voz integrada con el robot, profundidad con la RealSense real (`rgbd`) y con la escena completa del G1 en MuJoCo.
