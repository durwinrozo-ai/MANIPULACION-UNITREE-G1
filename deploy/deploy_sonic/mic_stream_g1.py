#!/usr/bin/env python3
"""Se ejecuta EN EL ROBOT (PC2, 192.168.123.164): captura el microfono USB (p. ej. Insta360)
con arecord y lo envia por UDP a la PC como PCM 16 kHz mono 16 bit (lo recibe voz_cajas.py --fuente udp).

    python3 mic_stream_g1.py --lista                       # tarjetas de audio de entrada
    python3 mic_stream_g1.py --tarjeta 2 --destino 192.168.123.222
    python3 mic_stream_g1.py --nombre Insta --destino 192.168.123.222

Solo usa la libreria estandar de Python y `arecord` (paquete alsa-utils: sudo apt install alsa-utils).
`plughw` convierte solo a 16 kHz mono, sea cual sea el formato nativo del microfono.
"""
import argparse
import os
import re
import socket
import subprocess
import sys


def arecord_l():
    try:
        return subprocess.run(["arecord", "-l"], capture_output=True, text=True).stdout
    except FileNotFoundError:
        print("No esta 'arecord'. Instalalo: sudo apt install alsa-utils")
        sys.exit(1)


INTERNAS = ("hdmi", "hda", "tegra", "nvidia", "dummy", "loopback")  # tarjetas internas: no son un microfono externo


def es_usb(linea):
    """True si la tarjeta de la linea de `arecord -l` esta en un puerto USB."""
    m = re.search(r"(\d+):", linea)
    if m and os.path.exists(f"/proc/asound/card{m.group(1)}/usbid"):
        return True
    return "usb" in linea.lower()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--lista", action="store_true")
    p.add_argument("--tarjeta", help="numero de tarjeta de arecord -l")
    p.add_argument("--dispositivo", default="0")
    p.add_argument("--nombre", help="opcional: parte del nombre de la tarjeta (varios, separados por coma). Sin nombre, 'auto' o si no "
                   "coincide, se usa cualquier microfono USB (de cualquier marca)")
    p.add_argument("--destino", default="192.168.123.222", help="IP de la PC en el cable")
    p.add_argument("--puerto", type=int, default=5600)
    a = p.parse_args()

    if a.lista:
        print(arecord_l() or "(sin dispositivos de captura)")
        return 0
    card = a.tarjeta
    if card is None:
        out = arecord_l()
        lineas = [l for l in out.splitlines() if l.lower().startswith(("tarjeta", "card"))]
        # 1) si se pidio un nombre concreto (puede ser una lista separada por comas), probarlo
        for nom in [n.strip() for n in (a.nombre or "").split(",") if n.strip() and n.strip().lower() != "auto"]:
            for line in lineas:
                if nom.lower() in line.lower():
                    card = re.search(r"(\d+):", line).group(1)
                    print(f"[mic] tarjeta elegida por nombre ({nom}): {line.strip()}")
                    break
            if card is not None:
                break
        # 2) sin nombre, 'auto' o nombre que no coincide: cualquier microfono, de cualquier marca, que este en un puerto USB
        if card is None:
            if a.nombre and a.nombre.strip().lower() != "auto":
                print(f"[mic] ninguna tarjeta se llama '{a.nombre}': busco cualquier microfono USB")
            usb = [l for l in lineas if es_usb(l)]
            cand = usb or [l for l in lineas if not any(x in l.lower() for x in INTERNAS)]
            if cand:
                if len(cand) > 1:
                    print("[mic] hay varias tarjetas de entrada; uso la primera (para otra: --nombre o --tarjeta):")
                    for l in cand:
                        print("       " + l.strip())
                card = re.search(r"(\d+):", cand[0]).group(1)
                print(f"[mic] tarjeta elegida (automatico): {cand[0].strip()}")
        if card is None:
            print("No encuentro ningun microfono. Tarjetas de entrada disponibles:")
            print(out or "(ninguna: el microfono no aparece; revisa el cable USB / receptor / que este encendido)")
            return 1
    dev = f"plughw:{card},{a.dispositivo}"
    cmd = ["arecord", "-D", dev, "-f", "S16_LE", "-r", "16000", "-c", "1", "-t", "raw", "-q"]
    print(f"[mic] {' '.join(cmd)}  ->  udp://{a.destino}:{a.puerto}  (Ctrl+C para parar)")
    arecord_l()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    n = 0
    try:
        while True:
            d = proc.stdout.read(3200)  # 100 ms
            if not d:
                print("[mic] arecord termino:", proc.stderr.read().decode(errors="replace").strip())
                return 1
            s.sendto(d, (a.destino, a.puerto))
            n += 1
            if n % 50 == 0:
                print(f"[mic] enviados {n} bloques de 100 ms")
    except KeyboardInterrupt:
        pass
    finally:
        proc.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
