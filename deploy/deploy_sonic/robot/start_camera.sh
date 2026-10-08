#!/bin/bash
# Libera la RealSense de quien la tenga (videohub_pc4 u otros) y arranca teleimager
sudo -v || exit 1
cd ~/teleimager
grep -q 'type: realsense' cam_config_server.yaml || echo "AVISO: cam_config_server.yaml no es realsense, revisalo"

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
teleimager-server --rs
