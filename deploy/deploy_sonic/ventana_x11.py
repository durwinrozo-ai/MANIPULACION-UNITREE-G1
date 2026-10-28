"""Coloca la ventana de MuJoCo con un tamano y una posicion razonables (solo X11 / XWayland).

El visor de MuJoCo abre su ventana al 2/3 de la pantalla y centrada, lo que en monitores grandes
parece pantalla completa. Este modulo busca la ventana por su titulo ("MuJoCo...") y la deja como
una ventana normal, mas pequena, que se puede mover con el mouse y redimensionar desde el borde.

Uso (desde run_sim_3colores.py):
    from ventana_x11 import ajustar_ventana_en_hilo
    ajustar_ventana_en_hilo()            # 1000x700 en (60, 60)
Variables de entorno:
    SIM_WIN=1000x700+60+60   tamano y posicion (formato WxH+X+Y; la posicion es opcional)
    SIM_WIN=off              no tocar la ventana
Si no hay X11 (Wayland puro, sin DISPLAY) no hace nada.
"""
import ctypes
import ctypes.util
import os
import re
import threading
import time

DEFECTO = (1000, 700, 60, 60)


def forzar_x11():
    """Obliga a GLFW (visor de MuJoCo) a usar X11/XWayland aunque la sesion sea Wayland.

    En Wayland nativo la ventana la dibuja el compositor y no se puede mover/redimensionar desde aqui;
    por XWayland si. Debe llamarse ANTES de abrir el visor. SIM_X11=0 lo desactiva.
    Devuelve True si hay DISPLAY (XWayland disponible) y se forzo X11."""
    if os.environ.get("SIM_X11", "1").strip().lower() in ("0", "off", "no", "false"):
        return False
    if not os.environ.get("DISPLAY"):
        return False  # sin XWayland no hay alternativa: se deja como esta
    os.environ.pop("WAYLAND_DISPLAY", None)      # GLFW 3.3 y 3.4 eligen X11 si no ven Wayland
    os.environ["XDG_SESSION_TYPE"] = "x11"
    os.environ["GDK_BACKEND"] = "x11"
    os.environ["SDL_VIDEODRIVER"] = "x11"
    try:  # GLFW >= 3.4: peticion explicita
        import glfw
        glfw.init_hint(glfw.PLATFORM, glfw.PLATFORM_X11)
    except Exception:
        pass
    return True


def leer_config(valor=None):
    """Devuelve (w, h, x, y) o None si esta desactivado."""
    valor = os.environ.get("SIM_WIN", "") if valor is None else valor
    valor = valor.strip().lower()
    if valor in ("off", "no", "0", "false"):
        return None
    m = re.fullmatch(r"(\d+)x(\d+)(?:\+(-?\d+)\+(-?\d+))?", valor)
    if not m:
        return DEFECTO
    w, h = int(m.group(1)), int(m.group(2))
    x = int(m.group(3)) if m.group(3) is not None else DEFECTO[2]
    y = int(m.group(4)) if m.group(4) is not None else DEFECTO[3]
    return w, h, x, y


class _XClientMessage(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int), ("serial", ctypes.c_ulong), ("send_event", ctypes.c_int),
                ("display", ctypes.c_void_p), ("window", ctypes.c_ulong), ("message_type", ctypes.c_ulong),
                ("format", ctypes.c_int), ("data", ctypes.c_long * 5)]


class _XEvent(ctypes.Union):
    _fields_ = [("xclient", _XClientMessage), ("pad", ctypes.c_long * 24)]


def _cargar_x11():
    ruta = ctypes.util.find_library("X11")
    if not ruta:
        return None
    x = ctypes.CDLL(ruta)
    x.XOpenDisplay.restype = ctypes.c_void_p
    x.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x.XDefaultRootWindow.restype = ctypes.c_ulong
    x.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
    x.XInternAtom.restype = ctypes.c_ulong
    x.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
    x.XQueryTree.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong),
                             ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.POINTER(ctypes.c_ulong)),
                             ctypes.POINTER(ctypes.c_uint)]
    x.XFetchName.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_char_p)]
    x.XFree.argtypes = [ctypes.c_void_p]
    x.XMoveResizeWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_uint, ctypes.c_uint]
    x.XSendEvent.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_long, ctypes.POINTER(_XEvent)]
    x.XChangeProperty.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
    x.XFlush.argtypes = [ctypes.c_void_p]
    x.XCloseDisplay.argtypes = [ctypes.c_void_p]
    return x


def _buscar(x, dpy, ventana, prefijo, nivel=0):
    """Busca (recursivo) una ventana cuyo titulo empiece por `prefijo`."""
    nombre = ctypes.c_char_p()
    if x.XFetchName(dpy, ventana, ctypes.byref(nombre)) and nombre.value:
        titulo = nombre.value.decode("utf-8", "ignore")
        x.XFree(nombre)
        if titulo.startswith(prefijo):
            return ventana
    if nivel > 3:
        return None
    raiz, padre = ctypes.c_ulong(), ctypes.c_ulong()
    hijos, n = ctypes.POINTER(ctypes.c_ulong)(), ctypes.c_uint()
    if not x.XQueryTree(dpy, ventana, ctypes.byref(raiz), ctypes.byref(padre), ctypes.byref(hijos), ctypes.byref(n)):
        return None
    try:
        for i in range(n.value):
            r = _buscar(x, dpy, hijos[i], prefijo, nivel + 1)
            if r:
                return r
    finally:
        if hijos:
            x.XFree(hijos)
    return None


def _mensaje(x, dpy, raiz, win, tipo, d0, d1=0, d2=0, d3=1):
    ev = _XEvent()
    ev.xclient.type = 33  # ClientMessage
    ev.xclient.send_event = 1
    ev.xclient.display = dpy
    ev.xclient.window = win
    ev.xclient.message_type = tipo
    ev.xclient.format = 32
    ev.xclient.data[0], ev.xclient.data[1], ev.xclient.data[2], ev.xclient.data[3] = d0, d1, d2, d3
    x.XSendEvent(dpy, raiz, 0, (1 << 20) | (1 << 19), ctypes.byref(ev))  # Substructure Redirect|Notify


def _ventana_normal(x, dpy, raiz, win):
    """Quita pantalla completa / maximizada y asegura el marco (barra de titulo, bordes)."""
    estado = x.XInternAtom(dpy, b"_NET_WM_STATE", 0)
    a = lambda n: x.XInternAtom(dpy, n, 0)
    _mensaje(x, dpy, raiz, win, estado, 0, a(b"_NET_WM_STATE_FULLSCREEN"))
    _mensaje(x, dpy, raiz, win, estado, 0, a(b"_NET_WM_STATE_MAXIMIZED_VERT"), a(b"_NET_WM_STATE_MAXIMIZED_HORZ"))
    motif = a(b"_MOTIF_WM_HINTS")
    hints = (ctypes.c_ulong * 5)(2, 0, 1, 0, 0)  # flags=decoraciones, decoraciones=todas
    x.XChangeProperty(dpy, win, motif, motif, 32, 0, ctypes.cast(hints, ctypes.c_void_p), 5)


def ajustar_ventana(cfg, prefijo="MuJoCo", espera=15.0):
    """Espera a que aparezca la ventana y la redimensiona/mueve. Devuelve True si lo logro."""
    if cfg is None or not os.environ.get("DISPLAY"):
        return False
    x = _cargar_x11()
    if x is None:
        return False
    dpy = x.XOpenDisplay(None)
    if not dpy:
        return False
    w, h, px, py = cfg
    try:
        raiz = x.XDefaultRootWindow(dpy)
        movres = x.XInternAtom(dpy, b"_NET_MOVERESIZE_WINDOW", 0)
        t0 = time.time()
        while time.time() - t0 < espera:
            win = _buscar(x, dpy, raiz, prefijo)
            if win:
                time.sleep(0.5)  # deja que el gestor de ventanas termine de colocarla
                _ventana_normal(x, dpy, raiz, win)
                x.XFlush(dpy)
                time.sleep(0.3)
                # 1) peticion al gestor de ventanas (gravedad 1 | x,y,w,h | origen "aplicacion" 1<<12)
                ev = _XEvent()
                ev.xclient.type = 33  # ClientMessage
                ev.xclient.send_event = 1
                ev.xclient.display = dpy
                ev.xclient.window = win
                ev.xclient.message_type = movres
                ev.xclient.format = 32
                ev.xclient.data[0] = 1 | (1 << 8) | (1 << 9) | (1 << 10) | (1 << 11) | (1 << 12)
                ev.xclient.data[1], ev.xclient.data[2] = px, py
                ev.xclient.data[3], ev.xclient.data[4] = w, h
                x.XSendEvent(dpy, raiz, 0, (1 << 20) | (1 << 19), ctypes.byref(ev))  # Substructure Redirect|Notify
                # 2) directo, por si no hay gestor de ventanas o no atiende la peticion
                x.XMoveResizeWindow(dpy, win, px, py, w, h)
                x.XFlush(dpy)
                return True
            time.sleep(0.3)
        return False
    finally:
        x.XCloseDisplay(dpy)


def ajustar_ventana_en_hilo(valor=None, prefijo="MuJoCo"):
    """Lanza `ajustar_ventana` en un hilo (no bloquea la simulacion)."""
    cfg = leer_config(valor)
    if cfg is None:
        return None
    t = threading.Thread(target=ajustar_ventana, args=(cfg, prefijo), daemon=True)
    t.start()
    return t
