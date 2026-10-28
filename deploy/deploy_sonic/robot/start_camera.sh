#!/bin/bash
# Libera la RealSense de quien la tenga (videohub_pc4 u otros) y arranca el servidor de camara.
#   bash ~/start_camera.sh          -> teleimager-server --rs  (solo RGB, :55555)   [validado]
#   bash ~/start_camera.sh rgbd     -> realsense_pub.py        (RGB :55555 + profundidad :55557)
MODO=${1:-teleimager}
export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"
sudo -v || exit 1
cd ~/teleimager

# cam_config_server.yaml vuelve a los valores de fabrica tras reiniciar el robot (sintoma: error de left_wrist_camera).
# Si head_camera no es realsense, se restaura la copia buena (~/cam_config_server.good.yaml) o se genera una.
CFG=cam_config_server.yaml
if [ "$MODO" != "rgbd" ] && ! grep -q 'type: realsense' $CFG 2>/dev/null; then
  cp -n $CFG ~/cam_config_server.factory.yaml 2>/dev/null
  if [ -f ~/cam_config_server.good.yaml ] && grep -q 'type: realsense' ~/cam_config_server.good.yaml; then
    cp ~/cam_config_server.good.yaml $CFG && echo "[cam] $CFG restaurado desde ~/cam_config_server.good.yaml"
  else
    cat > $CFG <<'YAML'
webrtc:
  bitrate:
    min: 2000000
    default: 5000000
    max: 12000000
  gop_length: 60

head_camera:
  enable_zmq: true
  zmq_port: 55555
  enable_webrtc: false
  webrtc_port: 60001
  webrtc_codec: h264
  type: realsense
  image_shape: [480, 640]
  binocular: false
  fps: 30
  video_id: 0
  serial_number: "347622070449"
  physical_path: null

left_wrist_camera:
  enable_zmq: false
  zmq_port: 55556
  enable_webrtc: false
  webrtc_port: 60002
  webrtc_codec: h264
  type: opencv
  image_shape: [480, 640]
  binocular: false
  fps: 30
  video_id: 2
  serial_number: null
  physical_path: null

right_wrist_camera:
  enable_zmq: false
  zmq_port: 55557
  enable_webrtc: false
  webrtc_port: 60003
  webrtc_codec: h264
  type: opencv
  image_shape: [480, 640]
  binocular: false
  fps: 30
  video_id: 4
  serial_number: null
  physical_path: null
YAML
    echo "[cam] $CFG regenerado (head_camera realsense, camaras de muneca desactivadas)"
  fi
  cp -n $CFG ~/cam_config_server.good.yaml 2>/dev/null
fi

liberar() {
  for p in $(sudo fuser /dev/video* 2>/dev/null); do
    n=$(ps -p $p -o comm= 2>/dev/null)
    case "$n" in python*|teleimager*) ;; *) echo "[cam] matando $p ($n)"; sudo kill -9 $p 2>/dev/null ;; esac
  done
  sudo pkill -9 -x videohub_pc4 2>/dev/null
}

# esperar a que quede libre 3 chequeos seguidos
ok=0
for i in $(seq 1 150); do
  liberar
  if [ -z "$(sudo fuser /dev/video* 2>/dev/null)" ]; then ok=$((ok+1)); else ok=0; fi
  [ $ok -ge 3 ] && break
  sleep 0.2
done

# seguir matando al que reaparezca mientras teleimager arranca (sin matar a python/teleimager)
( for i in $(seq 1 100); do liberar; sleep 0.2; done ) >/dev/null 2>&1 &
LOOP=$!
trap 'kill $LOOP 2>/dev/null' EXIT
if [ "$MODO" = "rgbd" ]; then
  python3 ~/realsense_pub.py
else
  teleimager-server --rs
fi
