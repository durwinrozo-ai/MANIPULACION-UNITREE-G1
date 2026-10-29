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



# rutas de red (PC con dos interfaces en la misma subred)
f,ax=fig(13,5.4)
ax.add_patch(FancyBboxPatch((5.0,0.9),4.4,3.8,boxstyle="round,pad=0.02,rounding_size=0.15",fc="#f7f9fc",ec="#9aa7b8",lw=1.5,ls="--"))
ax.text(7.2,4.4,"PC DEL LABORATORIO",ha="center",fontsize=10.5,weight="bold",color="#44505e")
box(ax,5.2,2.8,4.0,1.1,"enp131s0 · 192.168.123.222","blue",fs=9.5,sub="cable → robot")
box(ax,5.2,1.1,4.0,1.1,"wlp130s0f0 · 192.168.123.104","purple",fs=9.5,sub="Wi-Fi «zuunet» → lentes Pico")
box(ax,0.1,2.5,2.4,1.6,"Robot G1","gray",fs=10.5,sub="PC1 .161 · PC2 .164")
box(ax,10.6,0.9,2.3,1.6,"Pico 4","green",fs=10.5,sub="192.168.123.100\n(cambia por DHCP)")
arr(ax,(5.2,3.3),(2.5,3.3),"ruta de .164 y .161",c="blue",off=(0,0.18),fs=8.5)
arr(ax,(9.2,1.65),(10.6,1.65),"ruta del Pico",c="purple",off=(0,0.18),fs=8.5)
ax.text(6.5,0.45,"sudo ip route replace 192.168.123.164 dev enp131s0   ·   sudo ip route replace <IP del Pico> dev wlp130s0f0",ha="center",fontsize=8.6,color="#16202c",family="monospace")
ax.text(6.5,0.05,"Sin esas rutas Linux manda todo por el cable: las respuestas al Pico se pierden y no sale el aviso del certificado",ha="center",fontsize=8.8,color="#44505e")
ax.text(6.5,5.2,"Dos interfaces en la misma subred 192.168.123.0/24",ha="center",fontsize=12.5,weight="bold",color="#16202c")
f.savefig(O+"fig_rutas.png",dpi=170,bbox_inches="tight",facecolor="white");plt.close(f)

# ir al punto
f,ax=fig(12,5.0)
ax.add_patch(FancyBboxPatch((0.3,0.8),5.4,3.6,boxstyle="round,pad=0.02,rounding_size=0.08",fc="#eef1f5",ec="#5b6776",lw=1.8))
ax.text(3.0,4.55,"imagen de la cámara (0..1)",ha="center",fontsize=9,color="#44505e")
ax.plot([0.3,5.7],[0.8+3.6*(1-0.93),0.8+3.6*(1-0.93)][::1] if False else [4.4-3.6*0.93+0.0,4.4-3.6*0.93+0.0],color=C["red"],lw=1.5,ls="--")
yst=4.4-3.6*0.93
ax.text(5.65,yst+0.12,"y = 0.93 (parada)",ha="right",fontsize=8.5,color=C["red"],weight="bold")
for k,(x,y,al) in enumerate([(4.4,2.15,0.35),(3.7,1.75,0.6),(3.3,1.35,1.0)]):
    ax.plot([x],[y],"o",ms=10+4*k,mfc="none",mec=C["orange"],mew=2,alpha=al)
ax.annotate("",xy=(3.4,1.3),xytext=(4.3,2.1),arrowprops=dict(arrowstyle="-|>",color=C["orange"],lw=1.5,ls="--"))
ax.text(4.35,2.55,"punto elegido\n(gatillo derecho)",ha="center",fontsize=8.5,color=C["orange"],weight="bold")
ax.text(3.0,0.5,"al caminar, el punto baja y se acerca al borde",ha="center",fontsize=8.8,color="#44505e")
x0=6.4
box(ax,x0,3.4,5.3,1.0,"1  Apuntar y gatillo derecho","orange",fs=9.5,sub="cursor del Pico sobre el piso, sin caja debajo")
box(ax,x0,2.2,5.3,1.0,"2  Seguir el punto","blue",fs=9.5,sub="esquinas + flujo óptico (Lucas-Kanade) + RANSAC")
box(ax,x0,1.0,5.3,1.0,"3  Caminar y parar","green",fs=9.5,sub="gira hacia el punto, avanza; para en y ≥ 0.93")
arr(ax,(x0+2.65,3.4),(x0+2.65,3.2),None);arr(ax,(x0+2.65,2.2),(x0+2.65,2.0),None)
ax.text(x0+2.65,0.55,"Se detiene sola si pierde el punto, a los 30 s, o con PARAR",ha="center",fontsize=8.8,color=C["red"])
ax.text(6,4.95,"Ir a un punto del piso",ha="center",fontsize=12.5,weight="bold",color="#16202c")
f.savefig(O+"fig_punto.png",dpi=170,bbox_inches="tight",facecolor="white");plt.close(f)
