#!/bin/bash
# Libera la RealSense de quien la tenga (videohub_pc4 u otros) y arranca el servidor de camara.
#   bash ~/start_camera.sh          -> teleimager-server --rs  (solo RGB, :55555)   [validado]
#   bash ~/start_camera.sh rgbd     -> realsense_pub.py        (RGB :55555 + profundidad :55557)
MODO=${1:-teleimager}
export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"
sudo -v || exit 1
cd ~/teleimager
[ "$MODO" = "rgbd" ] || grep -q 'type: realsense' cam_config_server.yaml || echo "AVISO: cam_config_server.yaml no es realsense, revisalo"

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
