#!/usr/bin/env python3
"""Comandos de voz para el robot de las 3 cajas (microfono del G1 o del PC).

Convierte frases en las mismas teclas de deploy_sonic_vision.py:

    "zuu, busca el rojo"      -> 1        "zuu, ve al verde"  -> 2      "zuu, azul" -> 3
    "zuu, inspecciona"        -> s        "zuu, quieto"       -> 0
    "para" / "alto" / "detente" -> ESPACIO (PARAR; NO necesita la palabra de activacion)

Audio del G1: UDP multicast 239.168.123.161:5555, PCM 16 kHz mono 16 bit (mismo formato que usa
la app LSC en voz_a_sena/probar_microfono.py; viene de proyectos de terceros, no de la
documentacion oficial). El reconocimiento usa Google (SpeechRecognition) y necesita internet.

Probar SIN robot ni camara (solo oir y mostrar lo que entiende):
    python voz_cajas.py --fuente g1            # microfono del robot
    python voz_cajas.py --fuente pc            # microfono del PC
    python voz_cajas.py --fuente g1 --ip 192.168.123.222   # IP de la PC en el cable, si no se detecta sola

Requisitos (una vez):  pip install SpeechRecognition    (pc: ademas sounddevice)
"""
import argparse
import queue
import socket
import struct
import sys
import threading
import time
import unicodedata

import numpy as np

G1_MIC_GROUP = "239.168.123.161"
G1_MIC_PORT = 5555
RATE = 16000

STOP_WORDS = {"para", "parar", "alto", "detente", "detener", "detenerse", "frena", "frenar",
              "stop", "espera", "pausa", "basta"}
QUIET_WORDS = {"quieto", "quieta", "cero"}
SCAN_WORDS = {"inspecciona", "inspeccionar", "inspeccion", "gira", "girar", "explora", "explorar", "busca", "buscar"}
COLOR_WORDS = {"rojo": "1", "roja": "1", "verde": "2", "azul": "3"}
# Google puede transcribir "zuu" de varias formas; se aceptan las parecidas
WAKE_VARIANTS = {"zuu", "zuuu", "zu", "zuh", "suu", "su", "zoo", "zus"}


def normalizar(texto):
    t = unicodedata.normalize("NFD", texto.lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return "".join(c if c.isalnum() else " " for c in t).split()


def interpretar(texto, palabra_activacion="zuu"):
    """Devuelve (tecla, motivo). tecla es ' ', '0', 's', '1', '2', '3' o None.
    PARAR siempre funciona. El resto exige la palabra de activacion (si se configuro)."""
    w = normalizar(texto)
    if not w:
        return None, "vacio"
    ws = set(w)
    if ws & STOP_WORDS:
        return " ", "parar"
    if palabra_activacion:
        wake = {palabra_activacion.lower()} | (WAKE_VARIANTS if palabra_activacion.lower() == "zuu" else set())
        if not (ws & wake):
            return None, f"sin palabra de activacion '{palabra_activacion}'"
    colores = {COLOR_WORDS[x] for x in ws if x in COLOR_WORDS}
    if len(colores) > 1:
        return None, "varios colores: ambiguo"
    if colores:
        return colores.pop(), "color"
    if ws & QUIET_WORDS:
        return "0", "quieto"
    if ws & SCAN_WORDS:
        return "s", "inspeccionar"
    return None, "comando no reconocido"


def ip_hacia_robot(destino="192.168.123.161"):
    """IP de esta PC en la red del cable (la que usaria para llegar al robot)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((destino, 9))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


class G1MicSource(threading.Thread):
    """Recibe el audio del G1 por multicast y lo entrega como arrays int16 a on_audio()."""

    def __init__(self, on_audio, iface_ip=None):
        super().__init__(daemon=True)
        self.on_audio = on_audio
        self.iface_ip = iface_ip or ip_hacia_robot()
        self.packets = 0
        self.stop = False
        self.error = None

    def run(self):
        try:
            if not self.iface_ip:
                raise OSError("no se encontro la IP de la PC en 192.168.123.x (usa --voz-ip)")
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("", G1_MIC_PORT))
            mreq = socket.inet_aton(G1_MIC_GROUP) + socket.inet_aton(self.iface_ip)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            sock.settimeout(0.5)
        except OSError as e:
            self.error = str(e)
            return
        rest = b""
        while not self.stop:
            try:
                data, _ = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            self.packets += 1
            data = rest + data
            n = len(data) - (len(data) % 2)
            rest = data[n:]
            if n:
                self.on_audio(np.frombuffer(data[:n], dtype="<i2"))
        sock.close()


class PCMicSource:
    """Microfono de la PC con sounddevice."""

    def __init__(self, on_audio):
        import sounddevice as sd
        self.packets = 0
        self.error = None
        self._cb = on_audio

        def cb(indata, frames, t, status):
            self.packets += 1
            self._cb(indata[:, 0].copy())

        self.stream = sd.InputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=1600, callback=cb)

    def start(self):
        self.stream.start()

    @property
    def stop(self):
        return False

    @stop.setter
    def stop(self, v):
        if v:
            self.stream.stop()
            self.stream.close()


class VozListener:
    """Segmenta el audio en frases (por energia), las reconoce y deja las teclas en self.cmds."""

    def __init__(self, fuente="g1", iface_ip=None, idioma="es-CO", palabra_activacion="zuu",
                 umbral_min=500.0, verbose=True, reconocer=None):
        self.cmds = queue.Queue()
        self.idioma = idioma
        self.palabra = palabra_activacion
        self.umbral_min = umbral_min
        self.verbose = verbose
        self.reconocer = reconocer or self._reconocer_google
        self._audio_q = queue.Queue()
        self._frames = []
        self._pre = []
        self._speaking = False
        self._silence = 0.0
        self._floor = 200.0
        self.rms = 0.0
        self.thr = umbral_min
        self._buf = np.zeros(0, dtype=np.int16)
        self._stop = False
        self._sr = None
        if fuente == "g1":
            self.src = G1MicSource(self._on_audio, iface_ip)
        else:
            self.src = PCMicSource(self._on_audio)

    # -- audio -> frases ----------------------------------------------------------------
    def _on_audio(self, chunk):
        self._buf = np.concatenate([self._buf, chunk])
        step = int(RATE * 0.03)  # tramas de 30 ms
        while len(self._buf) >= step:
            frame, self._buf = self._buf[:step], self._buf[step:]
            self._procesar_trama(frame)

    def _procesar_trama(self, frame):
        dt = len(frame) / RATE
        rms = float(np.sqrt(np.mean(frame.astype(np.float64) ** 2)))
        self.rms = 0.8 * self.rms + 0.2 * rms
        self.thr = max(self.umbral_min, 3.0 * self._floor)
        if not self._speaking:
            self._floor = 0.98 * self._floor + 0.02 * rms
            self._pre.append(frame)
            if len(self._pre) > 10:
                self._pre.pop(0)
            if rms > self.thr:
                self._speaking = True
                self._frames = list(self._pre)
                self._silence = 0.0
        else:
            self._frames.append(frame)
            self._silence = self._silence + dt if rms < self.thr else 0.0
            dur = len(self._frames) * dt
            if self._silence >= 0.8 or dur >= 6.0:
                self._speaking = False
                if dur - self._silence >= 0.3:
                    self._audio_q.put(np.concatenate(self._frames).astype("<i2").tobytes())
                self._frames, self._pre = [], []

    # -- reconocimiento -------------------------------------------------------------------
    def _reconocer_google(self, pcm):
        if self._sr is None:
            import speech_recognition as sr
            self._sr = (sr, sr.Recognizer())
        sr, rec = self._sr
        return rec.recognize_google(sr.AudioData(pcm, RATE, 2), language=self.idioma)

    def _worker(self):
        while not self._stop:
            try:
                pcm = self._audio_q.get(timeout=0.3)
            except queue.Empty:
                continue
            try:
                texto = self.reconocer(pcm)
            except Exception as e:  # noqa: BLE001
                nombre = type(e).__name__
                if nombre == "UnknownValueError":
                    if self.verbose:
                        print("[Voz] (no se entendio)")
                elif nombre == "RequestError":
                    print(f"[Voz] sin internet o servicio no disponible: {e}")
                else:
                    print(f"[Voz] error de reconocimiento: {nombre}: {e}")
                continue
            tecla, motivo = interpretar(texto, self.palabra)
            if tecla is None:
                if self.verbose:
                    print(f'[Voz] oi: "{texto}" -> ignorado ({motivo})')
            else:
                print(f'[Voz] oi: "{texto}" -> comando {"ESPACIO" if tecla == " " else tecla} ({motivo})')
                self.cmds.put(tecla)

    def start(self):
        self.src.start()
        threading.Thread(target=self._worker, daemon=True).start()
        return self

    def close(self):
        self._stop = True
        self.src.stop = True

    def estado(self):
        return (f"paquetes={self.src.packets} nivel={self.rms:.0f} ruido={self._floor:.0f} "
                f"umbral={self.thr:.0f}{' [HABLANDO]' if self._speaking else ''}")


def main():
    p = argparse.ArgumentParser(description="Prueba de voz (sin robot ni camara)")
    p.add_argument("--fuente", choices=("g1", "pc"), default="g1")
    p.add_argument("--ip", default=None, help="IP de la PC en 192.168.123.x (multicast del G1)")
    p.add_argument("--idioma", default="es-CO")
    p.add_argument("--activacion", default="zuu", help="palabra de activacion ('' = sin palabra)")
    p.add_argument("--umbral", type=float, default=500.0, help="nivel RMS minimo para detectar voz")
    a = p.parse_args()
    v = VozListener(a.fuente, a.ip, a.idioma, a.activacion, a.umbral)
    v.start()
    print(f"[Voz] fuente={a.fuente} activacion='{a.activacion}'. Habla; Ctrl+C para salir.")
    t0 = time.time()
    try:
        while True:
            time.sleep(1.0)
            if v.src.error:
                print(f"[Voz] ERROR: {v.src.error}")
                break
            print("[Voz]", v.estado())
            if a.fuente == "g1" and time.time() - t0 > 4 and v.src.packets == 0:
                print("[Voz] AVISO: 0 paquetes del microfono del G1. Revisa cable/IP 192.168.123.x, "
                      "firewall UDP 5555 y usa --ip con la IP de la PC en el cable.")
            while not v.cmds.empty():
                v.cmds.get()
    except KeyboardInterrupt:
        pass
    v.close()


if __name__ == "__main__":
    sys.exit(main())
