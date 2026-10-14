import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch
from PIL import Image, ImageDraw, ImageFont
import os
O="img/"
C={"blue":"#2f6fdb","orange":"#d9780f","green":"#1f8f4e","red":"#d93a3a","gray":"#5b6776","purple":"#7a4fd1"}
BG={"blue":"#eaf1fd","orange":"#fdf2e4","green":"#e7f6ed","red":"#fdeaea","gray":"#eef1f5","purple":"#f0eafb"}
def box(ax,x,y,w,h,t,c="blue",fs=11,bold=True,sub=None):
    ax.add_patch(FancyBboxPatch((x,y),w,h,boxstyle="round,pad=0.02,rounding_size=0.12",fc=BG[c],ec=C[c],lw=2))
    if sub:
        ax.text(x+w/2,y+h*0.62,t,ha="center",va="center",fontsize=fs,weight="bold" if bold else None,color="#16202c")
        ax.text(x+w/2,y+h*0.27,sub,ha="center",va="center",fontsize=fs-2.2,color="#44505e")
    else:
        ax.text(x+w/2,y+h/2,t,ha="center",va="center",fontsize=fs,weight="bold" if bold else None,color="#16202c")
def arr(ax,a,b,t=None,c="gray",rad=0,fs=8.5,off=(0,0.12),ls="-"):
    ax.add_patch(FancyArrowPatch(a,b,arrowstyle="-|>",mutation_scale=16,lw=1.8,color=C[c],connectionstyle=f"arc3,rad={rad}",linestyle=ls))
    if t: ax.text((a[0]+b[0])/2+off[0],(a[1]+b[1])/2+off[1],t,ha="center",fontsize=fs,color=C[c],weight="bold")
def fig(w,h):
    f,ax=plt.subplots(figsize=(w,h));ax.set_xlim(0,w);ax.set_ylim(0,h);ax.axis("off");return f,ax


# 1 arquitectura
f,ax=fig(12,6.6)
ax.add_patch(FancyBboxPatch((0.2,0.4),3.7,5.8,boxstyle="round,pad=0.02,rounding_size=0.15",fc="#f7f9fc",ec="#9aa7b8",lw=1.5,ls="--"))
ax.text(2.05,5.92,"ROBOT G1",ha="center",fontsize=10.5,weight="bold",color="#44505e")
box(ax,0.45,3.9,3.2,1.6,"PC2  .164","blue",sub="RealSense D435i\nRGB :55555 · profundidad :55557")
box(ax,0.45,2.35,3.2,1.2,"Micrófono Insta360","purple",sub="USB-C · mic_stream_g1.py")
box(ax,0.45,0.75,3.2,1.2,"PC1  .161","gray",sub="control de motores")
ax.add_patch(FancyBboxPatch((4.5,0.4),7.4,5.8,boxstyle="round,pad=0.02,rounding_size=0.15",fc="#f7f9fc",ec="#9aa7b8",lw=1.5,ls="--"))
ax.text(8.2,5.92,"PC DEL LABORATORIO  (enp131s0 · .222)",ha="center",fontsize=10.5,weight="bold",color="#44505e")
box(ax,4.8,3.9,3.3,1.6,"Programa de cajas","green",sub="visión HSV + estados\nvoz (zuu) + teclas")
box(ax,4.8,0.75,3.3,1.2,"Deploy SONIC","orange",sub="política 29 GDL · deploy.sh")
box(ax,8.7,0.75,3.0,4.75,"","red")
ax.text(10.2,4.85,"PANEL WEB",ha="center",fontsize=12.5,weight="bold",color="#16202c")
ax.text(10.2,4.4,"127.0.0.1:8080",ha="center",fontsize=9.5,color="#44505e")
ax.text(10.2,2.9,"lanza y detiene\ncada proceso\n\nvideo RGB / profundidad\nbotones · voz\nemergencia",ha="center",va="center",fontsize=9.5,color="#44505e")
arr(ax,(3.65,4.7),(4.8,4.7),"JPEG ZMQ",c="blue",off=(0,0.15))
arr(ax,(3.65,3.2),(5.0,3.9),None,c="purple")
ax.text(3.7,2.95,"audio UDP :5600",fontsize=8.5,color=C["purple"],weight="bold",ha="left",va="top")
arr(ax,(6.45,3.9),(6.45,1.95),None,c="green")
ax.text(6.6,2.95,"planner ZMQ :5556",fontsize=8.5,color=C["green"],weight="bold")
arr(ax,(4.8,1.35),(3.65,1.35),"movimiento",c="orange",off=(0,0.17))
arr(ax,(8.7,4.7),(8.1,4.7),None,c="red",ls="--")
arr(ax,(8.7,1.35),(8.1,1.35),None,c="red",ls="--")
f.savefig(O+"fig_arq.png",dpi=170,bbox_inches="tight",facecolor="white");plt.close(f)

# 2 orden

f,ax=fig(12,4.3)
names=[("A","Cámara","ssh PC2","start_camera.sh","head_camera is ready","blue"),
       ("M","Micrófono","ssh PC2","mic_stream_g1.py","[mic] enviados N bloques","purple"),
       ("2","Deploy SONIC","PC","deploy.sh ... enp131s0","Init Done  (responder y)","orange"),
       ("3","Programa de cajas","PC","deploy_sonic_vision.py --real","escribir SI → Planner enabled","green")]
x=0.2
for i,(k,t,w,cmd,ready,c) in enumerate(names):
    box(ax,x,1.2,2.6,2.6,"","gray")
    ax.add_patch(plt.Circle((x+0.45,3.3),0.3,fc=C[c],ec="none"));ax.text(x+0.45,3.3,k,ha="center",va="center",color="white",fontsize=14,weight="bold")
    ax.text(x+0.9,3.3,t,va="center",fontsize=11,weight="bold",color="#16202c")
    ax.text(x+0.15,2.75,w,fontsize=9,color="#44505e")
    ax.text(x+0.15,2.3,cmd,fontsize=8.2,family="monospace",color="#16202c")
    ax.text(x+0.15,1.65,"listo cuando:",fontsize=8,color="#44505e")
    ax.text(x+0.15,1.38,ready,fontsize=8.2,weight="bold",color=C[c])
    if i<3: arr(ax,(x+2.6,2.5),(x+3.0,2.5),None,c="gray")
    x+=3.0
ax.text(6,0.55,"Cierre: ESPACIO → q en la 3 → Ctrl+C en 2 → Ctrl+C en M y A   (el panel lo hace con «Detener todo»)",ha="center",fontsize=10,color=C["red"],weight="bold")
ax.text(6,4.1,"Orden de arranque (robot real)",ha="center",fontsize=12.5,weight="bold",color="#16202c")
f.savefig(O+"fig_orden.png",dpi=170,bbox_inches="tight",facecolor="white");plt.close(f)


# 3 estados (con histeresis)
f,ax=fig(12,5.2)
box(ax,0.3,2.0,2.1,1.2,"QUIETO","gray",sub="IDLE")
box(ax,3.2,3.3,2.8,1.3,"INSPECCIONANDO","blue",sub="IDLE + facing gira\n0.3 rad/s")
box(ax,3.2,0.6,2.8,1.4,"GIRANDO EN SITIO","orange",sub="IDLE hasta |error| < 0.2\n(solo si |error| > 0.65)")
box(ax,6.8,2.0,2.8,1.4,"CAMINANDO","green",sub="SLOW_WALK 0.3 m/s\n(0.15 si y_max>0.80)\ncorrige el rumbo sin parar")
box(ax,10.2,2.0,1.6,1.3,"LLEGÓ","red",sub="y_max ≥ 0.93")
arr(ax,(2.4,2.9),(3.2,3.8),"s",c="blue")
arr(ax,(2.4,2.3),(3.2,1.4),"1 / 2 / 3",c="orange",off=(-0.3,-0.3))
arr(ax,(6.0,4.0),(7.4,3.4),"ve el color",c="green",off=(0.3,0.2))
arr(ax,(6.0,1.8),(6.8,2.4),"centrado",c="green",off=(-0.25,0.25))
arr(ax,(7.6,2.0),(6.0,1.0),"muy desviada (>0.65)",c="orange",off=(0.55,-0.35),fs=8.5)
arr(ax,(9.6,2.7),(10.2,2.7),None,c="red")
arr(ax,(11.0,2.0),(1.4,2.0),None,c="gray",rad=-0.35)
ax.text(6,-0.35,"0  ·  ESPACIO  ·  q   (vuelve a QUIETO desde cualquier estado)",ha="center",fontsize=9.5,weight="bold",color=C["gray"])
ax.set_ylim(-0.8,5.2)
ax.text(6,5.0,"Máquina de estados del programa de cajas",ha="center",fontsize=12.5,weight="bold",color="#16202c")
f.savefig(O+"fig_estados.png",dpi=170,bbox_inches="tight",facecolor="white");plt.close(f)

# 4 red

f,ax=fig(12,5.4)
ax.text(6,5.2,"Panel con puntos rojos / «No route to host»: qué revisar",ha="center",fontsize=12.5,weight="bold",color="#16202c")
box(ax,0.3,3.9,3.2,0.9,"¿Robot encendido y\ncon cable?","gray",fs=10)
box(ax,4.4,3.9,3.4,0.9,"ip -br addr show enp131s0","blue",fs=9.5)
box(ax,8.7,3.9,3.0,0.9,"ping .161 y .164","green",fs=10)
arr(ax,(3.5,4.35),(4.4,4.35),None);arr(ax,(7.8,4.35),(8.7,4.35),None)
box(ax,0.3,1.9,3.2,1.3,"No: encender, revisar cable/switch\ny esperar 30-60 s","red",fs=8.8,bold=False)
box(ax,4.4,1.9,3.4,1.3,"NO-CARRIER/DOWN → cable\nsin IP → ip addr add .222/24\nsin interfaz → ip -br link","red",fs=8.8,bold=False)
box(ax,8.7,1.9,3.0,1.3,"Sin respuesta → revisar ruta:\nip route get 192.168.123.164\n(debe salir por enp131s0)","red",fs=8.8,bold=False)
for xx in (1.9,6.1,10.2): arr(ax,(xx,3.9),(xx,3.2),"falla",c="red",off=(0.35,0))
ax.text(6,1.0,"Todo en verde → «Preparar robot (scp)» → «Arrancar todo»",ha="center",fontsize=11,weight="bold",color=C["green"])
f.savefig(O+"fig_red.png",dpi=170,bbox_inches="tight",facecolor="white");plt.close(f)


# 5 voz

f,ax=fig(12,3.6)
steps=[("Insta360\nMic Air RX","USB-C al robot","purple"),("mic_stream_g1.py\n(PC2)","arecord 16 kHz","blue"),("UDP :5600","por el cable","gray"),
       ("voz_cajas.py\n(PC)","energía RMS +\nGoogle Speech","orange"),("interpretar()","«zuu, busca el rojo»\n→ tecla 1","green"),("Programa de cajas","misma lógica\nque el teclado","red")]
x=0.15
for i,(t,s,c) in enumerate(steps):
    box(ax,x,1.2,1.75,1.7,t,c,fs=9,sub=s)
    if i<5: arr(ax,(x+1.75,2.05),(x+2.0,2.05),None)
    x+=2.0
ax.text(6,3.3,"Cadena de voz",ha="center",fontsize=12.5,weight="bold",color="#16202c")
ax.text(6,0.55,"«zuu, busca el rojo/verde/azul» · «zuu, inspecciona» · «zuu, quieto»  ·  «para» / «alto» = PARAR (no es la parada de emergencia)",ha="center",fontsize=9,color="#44505e")
f.savefig(O+"fig_voz.png",dpi=170,bbox_inches="tight",facecolor="white");plt.close(f)


# 6 marcha continua (ilustrativo)
import numpy as np
t=np.linspace(0,12,600)
err=0.36+0.17*np.sin(t*2.1)-0.025*t
def modes(err,ct,tt):
    m=[];turn=False
    for e in err:
        e=abs(e)
        if turn:
            if e<ct: turn=False
        elif e>tt: turn=True
        m.append(0 if turn else 1)
    return np.array(m)
old=np.array([0 if abs(e)>0.35 else 1 for e in err]); new=modes(err,0.2,0.65)
f,(a1,a2)=plt.subplots(2,1,figsize=(11,5.4),gridspec_kw={"height_ratios":[2.2,1]},sharex=True)
a1.plot(t,np.abs(err),color=C["gray"],lw=2,label="|error| de la caja en la imagen (ilustrativo)")
a1.axhline(0.35,color=C["red"],ls="--",lw=1.5);a1.text(12.05,0.37,"antes: parar si > 0.35",color=C["red"],fontsize=9,va="bottom")
a1.axhline(0.65,color=C["green"],ls="-",lw=1.8);a1.text(12.05,0.65,"ahora: girar\nsi > 0.65",color=C["green"],fontsize=9,va="center")
a1.axhline(0.2,color=C["green"],ls=":",lw=1.8);a1.text(12.05,0.2,"ahora: caminar\nsi < 0.2",color=C["green"],fontsize=9,va="center")
a1.set_ylabel("|error|");a1.legend(loc="upper right",fontsize=9,frameon=False);a1.set_ylim(0,0.8)
for a in (a1,a2):
    for sp in ("top","right"): a.spines[sp].set_visible(False)
a2.fill_between(t,0,1,where=old==1,step="mid",color=C["red"],alpha=.35,label="antes")
a2.fill_between(t,1.2,2.2,where=new==1,step="mid",color=C["green"],alpha=.45,label="ahora")
a2.set_yticks([0.5,1.7]);a2.set_yticklabels(["antes","ahora"]);a2.set_xlabel("tiempo (s)")
a2.text(12.05,1.7,"camina (SLOW_WALK)",fontsize=8.5,color=C["green"],va="center");a2.text(12.05,0.5,"hueco = parado (IDLE)",fontsize=8.5,color=C["red"],va="center")
a1.set_title("Marcha continua: el giro en sitio ahora tiene histéresis",fontsize=12.5,weight="bold",color="#16202c")
f.savefig(O+"fig_marcha.png",dpi=170,bbox_inches="tight",facecolor="white");plt.close(f)

# 7 camaras
f,ax=fig(12,4.6)
ax.text(6,4.4,"Cámaras RGB y profundidad: de dónde sale cada una",ha="center",fontsize=12.5,weight="bold",color="#16202c")
box(ax,0.3,2.6,3.0,1.3,"ROBOT REAL · PC2","blue",sub="realsense_pub.py (opción\n«RealSense propio»)")
box(ax,0.3,0.6,3.0,1.3,"SIMULADOR MUJOCO","purple",sub="run_sim_3colores.py\n+ sim_depth.py (SIM_DEPTH=1)")
box(ax,4.5,2.6,2.6,1.3,"RGB (JPEG)","green",sub="real :55555")
box(ax,4.5,0.6,2.6,1.3,"RGB (JPEG)","green",sub="sim :5555")
box(ax,7.5,2.6,2.2,1.3,"Profundidad","orange",sub="PNG16 mm · real :55557")
box(ax,7.5,0.6,2.2,1.3,"Profundidad","orange",sub="PNG16 mm · sim :5567")
box(ax,10.1,0.6,1.7,3.3,"","red")
ax.text(10.95,3.3,"PANEL",ha="center",fontsize=12,weight="bold",color="#16202c")
ax.text(10.95,2.1,"RGB\nProfundidad\nRGB + prof.\nOcultar",ha="center",va="center",fontsize=9.5,color="#44505e")
for y in (3.25,1.25):
    arr(ax,(3.3,y),(4.5,y),None,c="gray");arr(ax,(7.1,y),(7.5,y),None,c="gray");arr(ax,(9.7,y),(10.1,y),None,c="gray")
f.savefig(O+"fig_camaras.png",dpi=170,bbox_inches="tight",facecolor="white");plt.close(f)

# panel anotado (captura del panel actual)
im=Image.open("panel_depth.png").convert("RGB");d=ImageDraw.Draw(im)
try: ft=ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",22)
except: ft=None
marks=[(1,(238,58)),(2,(640,58)),(3,(1013,58)),(4,(1310,58)),(5,(700,91)),(6,(600,426)),(7,(700,559)),(8,(700,757)),(9,(1350,91)),(10,(1350,401)),(11,(1350,797))]
for n,(x,y) in marks:
    d.ellipse((x-17,y-17,x+17,y+17),fill="#d93a3a",outline="white",width=3)
    d.text((x,y),str(n),fill="white",font=ft,anchor="mm")
im.save(O+"panel_anotado.png")
Image.open("/root/.claude/uploads/09c60ef0-70e9-599b-9201-e06bef8b371e/63ee2c99-image.png").convert("RGB").crop((1117,175,1880,820)).save(O+"mujoco_ventana.png")
