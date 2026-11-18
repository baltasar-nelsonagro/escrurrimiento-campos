#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Análisis de escurrimiento de un campo a partir de su contorno (KML/KMZ).

Uso:
    python3 analisis_escurrimiento.py camino/al/campo.kml
    python3 analisis_escurrimiento.py campo.kml --buffer 3 --salida ./resultados
    python3 analisis_escurrimiento.py campo.kml --dem mi_dem_5m.tif   (usar un DEM propio, p.ej. IGN 5m)

Qué hace:
  1. Lee el contorno del campo (polígono) del KML/KMZ.
  2. Baja solo el Modelo de Elevación Copernicus GLO-30 (30 m) de esa zona
     desde el repositorio abierto de AWS (gratis, sin clave).  [se omite con --dem]
  3. Corre el análisis hidrológico: acondicionamiento + dirección de flujo D8 + acumulación.
  4. Recorta al contorno del campo y detecta salidas de agua, bajos anegables y lomas.
  5. Genera: mapa PNG, archivo KMZ para Google Earth, e informe PDF con recomendaciones.

Requiere (ver requirements.txt): numpy, scipy, rasterio, shapely, pysheds, matplotlib, pillow, requests
"""
import os, sys, math, json, zipfile, argparse, tempfile, urllib.request, urllib.error
import numpy as np

# --- Compatibilidad numpy 2.0 con pysheds (in1d -> isin) ---
if not hasattr(np, "in1d"):
    np.in1d = np.isin

COP_BASE = "https://copernicus-dem-30m.s3.amazonaws.com"
CELL_HA = 0.09  # 30 m x 30 m = 900 m2 = 0.09 ha (aprox)


def _contexto_ssl():
    """Devuelve un contexto SSL con certificados válidos.
    Soluciona el error 'unable to get local issuer certificate' típico de
    Python en macOS, instalando certifi al vuelo si hiciera falta."""
    import ssl
    try:
        import certifi
    except ImportError:
        import subprocess
        print("   · Instalando certificados (certifi) por única vez ...")
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "certifi"],
                       check=False)
        try:
            import certifi
        except ImportError:
            print("   · Aviso: no se pudo instalar certifi; se usa el contexto por defecto.")
            return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


# ----------------------------------------------------------------------------
# 1. Lectura del contorno (KML / KMZ)
# ----------------------------------------------------------------------------
def _parsear_coords(texto):
    pts = []
    for token in (texto or "").replace("\n", " ").split():
        partes = token.split(",")
        if len(partes) >= 2:
            try:
                pts.append((float(partes[0]), float(partes[1])))
            except ValueError:
                pass
    return pts


def _limpiar_poly(poly):
    """Devuelve un Polygon válido (si es MultiPolygon, toma el de mayor área)."""
    if poly.is_empty:
        return None
    if not poly.is_valid:
        poly = poly.buffer(0)
    if poly.is_empty:
        return None
    if poly.geom_type == "MultiPolygon":
        poly = max(poly.geoms, key=lambda g: g.area)
    if poly.geom_type != "Polygon" or poly.area == 0:
        return None
    return poly


def leer_contornos(path):
    """Lee TODOS los campos (polígonos) de un KML/KMZ.
    Devuelve una lista de dicts: [{'poly': Polygon, 'nombre': str}]."""
    import xml.etree.ElementTree as ET
    from shapely.geometry import Polygon
    if path.lower().endswith(".kmz"):
        with zipfile.ZipFile(path) as z:
            nombre_kml = next((n for n in z.namelist() if n.lower().endswith(".kml")), None)
            if not nombre_kml:
                raise SystemExit("El KMZ no contiene un .kml adentro.")
            texto = z.read(nombre_kml).decode("utf-8", "ignore")
    else:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            texto = f.read()

    try:
        root = ET.fromstring(texto)
    except ET.ParseError:
        import re
        texto = re.sub(r'\sxmlns(:\w+)?="[^"]+"', "", texto)
        texto = re.sub(r"<(/?)[A-Za-z0-9_]+:", r"<\1", texto)
        texto = re.sub(r'\s[A-Za-z0-9_]+:([A-Za-z0-9_]+=)', r" \1", texto)
        root = ET.fromstring(texto)

    campos = []
    # Un campo = un Placemark con Polygon (así asociamos cada nombre a su polígono)
    for pm in root.findall(".//{*}Placemark"):
        polyel = pm.find(".//{*}Polygon")
        if polyel is None:
            continue
        coords_el = polyel.find(".//{*}outerBoundaryIs//{*}coordinates")
        if coords_el is None:
            coords_el = polyel.find(".//{*}coordinates")
        pts = _parsear_coords(coords_el.text if coords_el is not None else "")
        if len(pts) < 3:
            continue
        poly = _limpiar_poly(Polygon(pts))
        if poly is None:
            continue
        n = pm.find("{*}name")
        nombre = n.text.strip() if (n is not None and n.text and n.text.strip()) else None
        campos.append({"poly": poly, "nombre": nombre})

    # Fallback: KML sin Placemarks claros -> cualquier polígono suelto
    if not campos:
        for coords in root.findall(".//{*}Polygon//{*}coordinates"):
            pts = _parsear_coords(coords.text)
            if len(pts) >= 3:
                poly = _limpiar_poly(Polygon(pts))
                if poly is not None:
                    campos.append({"poly": poly, "nombre": None})

    if not campos:
        raise SystemExit("No se encontró ningún polígono en el archivo. "
                         "Asegurate de dibujar el contorno como polígono y exportarlo como KML.")

    # Nombres: completar los vacíos y resolver duplicados
    vistos = {}
    for i, c in enumerate(campos):
        base = c["nombre"] or f"Campo {i+1}"
        base = "".join(ch for ch in base if ch.isalnum() or ch in " -_").strip() or f"Campo {i+1}"
        if base in vistos:
            vistos[base] += 1
            base = f"{base} ({vistos[base]})"
        else:
            vistos[base] = 1
        c["nombre"] = base
    return campos


def nombre_campo(path):
    base = os.path.splitext(os.path.basename(path))[0]
    return base.replace("_", " ").strip() or "Campo"


# ----------------------------------------------------------------------------
# 2. Descarga del DEM Copernicus GLO-30
# ----------------------------------------------------------------------------
def tiles_para_bbox(W, S, E, N):
    nombres = []
    for lat in range(math.floor(S), math.floor(N) + 1):
        for lon in range(math.floor(W), math.floor(E) + 1):
            ns = "N" if lat >= 0 else "S"
            ew = "E" if lon >= 0 else "W"
            nombres.append(f"Copernicus_DSM_COG_10_{ns}{abs(lat):02d}_00_{ew}{abs(lon):03d}_00_DEM")
    return nombres


def _bajar_tile(url, destino, ctx, intentos=3):
    """Baja un tile con reintentos y descarga por partes. Devuelve True/False.
    Lanza FileNotFoundError si el tile no existe (404)."""
    import shutil
    ultimo_error = None
    for intento in range(1, intentos + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "escurrimiento/1.0"})
            with urllib.request.urlopen(req, timeout=300, context=ctx) as r, open(destino, "wb") as f:
                shutil.copyfileobj(r, f, length=1024 * 256)
            if os.path.getsize(destino) > 0:
                print(f"     ok ({os.path.getsize(destino)//(1024*1024)} MB)")
                return True
            ultimo_error = "archivo vacío"
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise FileNotFoundError(url)
            ultimo_error = f"HTTP {e.code}"
        except Exception as e:
            ultimo_error = str(e)
        if intento < intentos:
            print(f"     reintentando ({intento}/{intentos-1})... [{ultimo_error}]")
    raise RuntimeError(f"no se pudo bajar el tile tras {intentos} intentos: {ultimo_error}")


def descargar_dem(bbox, carpeta_tmp):
    import rasterio
    from rasterio.merge import merge
    W, S, E, N = bbox
    tiles = tiles_para_bbox(W, S, E, N)
    ctx = _contexto_ssl()
    rasters = []
    faltantes = []
    for t in tiles:
        url = f"{COP_BASE}/{t}/{t}.tif"
        destino = os.path.join(carpeta_tmp, t + ".tif")
        if os.path.exists(destino) and os.path.getsize(destino) > 0:
            print(f"   · {t} ya descargado (reuso)")
            rasters.append(rasterio.open(destino)); continue
        print(f"   · Bajando {t} ...")
        try:
            _bajar_tile(url, destino, ctx)
            rasters.append(rasterio.open(destino))
        except FileNotFoundError:
            print(f"     (sin datos para {t} — probablemente agua; se omite)")
            faltantes.append(t)
    if not rasters:
        raise RuntimeError("no se pudo bajar el mapa de elevación de esa zona "
                           "(revisá la conexión a internet y volvé a intentar)")
    mosaico, transform = merge(rasters, bounds=(W, S, E, N))
    meta = rasters[0].meta.copy()
    for r in rasters:
        r.close()
    meta.update({"height": mosaico.shape[1], "width": mosaico.shape[2],
                 "transform": transform, "count": 1})
    salida = os.path.join(carpeta_tmp, "dem_recortado.tif")
    with rasterio.open(salida, "w", **meta) as dst:
        dst.write(mosaico[0], 1)
    return salida


# ----------------------------------------------------------------------------
# 3. Análisis hidrológico
# ----------------------------------------------------------------------------
def correr_flujo(dem_path):
    from pysheds.grid import Grid
    grid = Grid.from_raster(dem_path)
    dem = grid.read_raster(dem_path)
    dem = grid.fill_pits(dem)
    dem = grid.fill_depressions(dem)
    inflated = grid.resolve_flats(dem)
    dirmap = (64, 128, 1, 2, 4, 8, 16, 32)
    fdir = grid.flowdir(inflated, dirmap=dirmap)
    acc = grid.accumulation(fdir, dirmap=dirmap)
    import rasterio
    with rasterio.open(dem_path) as ds:
        bounds = (ds.bounds.left, ds.bounds.bottom, ds.bounds.right, ds.bounds.top)
    demraw = np.asarray(grid.read_raster(dem_path))
    return np.asarray(acc), demraw, bounds


def rasterizar_campo(campo, shape, bounds):
    from rasterio.features import rasterize
    from rasterio.transform import from_bounds
    H, W = shape
    transform = from_bounds(bounds[0], bounds[1], bounds[2], bounds[3], W, H)
    mask = rasterize([(campo.__geo_interface__, 1)], out_shape=(H, W),
                     transform=transform, fill=0, dtype="uint8").astype(bool)
    return mask


# ----------------------------------------------------------------------------
# 4. Cálculo de estadísticas y salidas
# ----------------------------------------------------------------------------
def analizar(acc, dem, field, bounds):
    from scipy.ndimage import binary_erosion
    H, W = acc.shape
    inside = dem[field]
    datos = {}
    datos["ha_campo"] = round(field.sum() * CELL_HA)
    datos["elev_min"] = round(float(inside.min()), 1)
    datos["elev_max"] = round(float(inside.max()), 1)
    datos["elev_med"] = round(float(np.median(inside)), 1)
    datos["desnivel"] = round(float(inside.max() - inside.min()), 1)
    p = np.percentile(inside, [15, 40, 70])
    muy_bajo = field & (dem < p[0])
    bajo = field & (dem >= p[0]) & (dem < p[1])
    alto = field & (dem >= p[2])
    datos["ha_anegable"] = round(muy_bajo.sum() * CELL_HA)
    datos["ha_alto"] = round(alto.sum() * CELL_HA)
    datos["acc_max_ha"] = round(float(acc.max()) * CELL_HA)

    # Salidas: celdas del borde interno con alta acumulación, agrupadas
    inner = binary_erosion(field, iterations=1)
    borde = field & ~inner
    umbral = max(150, float(np.percentile(acc[field], 99.5)) * 0.3)
    cand = np.argwhere(borde & (acc > umbral))
    cx = field.nonzero()[1].mean(); cy = field.nonzero()[0].mean()
    def rc_to_lonlat(r, c):
        lon = bounds[0] + (c + 0.5) / W * (bounds[2] - bounds[0])
        lat = bounds[3] - (r + 0.5) / H * (bounds[3] - bounds[1])
        return lon, lat
    def rumbo(r, c):
        dx = c - cx; dy = cy - r  # dy positivo = hacia el norte
        ang = math.degrees(math.atan2(dy, dx))
        dirs = [("E", 0), ("NE", 45), ("N", 90), ("NO", 135),
                ("O", 180), ("SO", -135), ("S", -90), ("SE", -45)]
        best = min(dirs, key=lambda d: abs((ang - d[1] + 180) % 360 - 180))
        return best[0]
    salidas = []
    for r, c in cand:
        lon, lat = rc_to_lonlat(r, c)
        salidas.append({"lon": round(lon, 5), "lat": round(lat, 5),
                        "rumbo": rumbo(r, c), "acc_ha": round(float(acc[r, c]) * CELL_HA)})
    # Agrupar por rumbo: quedarse con la de mayor acumulación por dirección
    por_rumbo = {}
    for s in salidas:
        if s["rumbo"] not in por_rumbo or s["acc_ha"] > por_rumbo[s["rumbo"]]["acc_ha"]:
            por_rumbo[s["rumbo"]] = s
    principales = sorted(por_rumbo.values(), key=lambda s: -s["acc_ha"])[:3]
    datos["salidas"] = principales
    return datos, muy_bajo, bajo, alto


def narrar(nombre, datos):
    sal = datos["salidas"]
    if sal:
        princ = sal[0]
        lista = ", ".join(f"{s['rumbo']} (~{s['acc_ha']} ha)" for s in sal)
        txt_salida = (f"El agua escurre y sale principalmente hacia el {princ['rumbo']} "
                      f"(cuenca de ~{princ['acc_ha']} ha). Salidas detectadas: {lista}.")
        rep = (f"Represa / tajamar / aguada: el mejor lugar es sobre la salida {princ['rumbo']}, "
               f"donde el agua llega sola (~{princ['acc_ha']} ha de cuenca); capturás escurrimiento sin bombear.")
    else:
        txt_salida = "No se detectaron cauces concentrados marcados (terreno muy parejo)."
        rep = "Represa / tajamar: buscar las depresiones suaves donde converge el escurrimiento menor."
    diag = [
        txt_salida,
        f"El campo es {'muy plano' if datos['desnivel'] < 15 else 'suavemente ondulado'}: "
        f"{datos['desnivel']} m de desnivel total ({datos['elev_min']} a {datos['elev_max']} m).",
        f"Zonas bajas / anegables: ~{datos['ha_anegable']} ha "
        f"({round(100*datos['ha_anegable']/max(datos['ha_campo'],1))}% del campo): se tapan primero con una lluvia fuerte.",
        f"Zonas altas / lomas bien drenadas: ~{datos['ha_alto']} ha.",
    ]
    reco = [
        rep,
        "Casco, corrales, caminos, manga, feedlot: ubicarlos en las lomas altas (lo marrón del mapa); escurren y no se empantanan.",
        "No ubicar sobre los cauces ni los bajos nada que no deba quedar bajo agua (silos a nivel de piso, acopios, corrales permanentes).",
        "Caminos internos: no cruzar un cauce con terraplén sin alcantarilla — genera represa aguas arriba y corte de camino aguas abajo.",
    ]
    return diag, reco


# ----------------------------------------------------------------------------
# 5. Salidas gráficas: PNG, KMZ, PDF
# ----------------------------------------------------------------------------
def generar_mapa(acc, dem, field, bounds, nombre, datos, png_path, campo=None):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import LightSource, LinearSegmentedColormap, ListedColormap
    from matplotlib.lines import Line2D
    from matplotlib.patches import Patch
    from scipy.ndimage import gaussian_filter, binary_dilation
    H, W = acc.shape
    extent = [bounds[0], bounds[2], bounds[1], bounds[3]]
    demf = gaussian_filter(dem, 0.6)
    ls = LightSource(azdeg=315, altdeg=45)
    hs = ls.hillshade(demf, vert_exag=55, dx=30, dy=30)
    terr = LinearSegmentedColormap.from_list("terr",
            ["#1b6b4a", "#4a9b5e", "#9fc26b", "#d9c98a", "#c9a96a", "#a9815a"])
    inside = dem[field]; dmin, dmax = np.percentile(inside, 2), np.percentile(inside, 98)
    fig, ax = plt.subplots(figsize=(10.5, 12), dpi=150)
    ax.imshow(hs, cmap="gray", extent=extent, origin="upper")
    tint = np.ma.masked_where(~field, np.clip((dem - dmin) / (dmax - dmin + 1e-9), 0, 1))
    ax.imshow(tint, cmap=terr, extent=extent, origin="upper", alpha=0.55)
    out = np.ma.masked_where(field, np.ones_like(dem))
    ax.imshow(out, extent=extent, origin="upper", cmap=ListedColormap(["#000000"]), alpha=0.38)
    def ov(mask, color, a):
        m = np.ma.masked_where(~mask, np.ones_like(acc))
        ax.imshow(m, extent=extent, origin="upper", cmap=ListedColormap([color]), alpha=a)
    ov(field & (acc > 80), "#5ab0ff", 0.6)
    ov(field & (acc > 800), "#1565d8", 0.9)
    ov(field & binary_dilation(acc > 4000), "#07306f", 1.0)
    # contorno del campo
    if campo is not None:
        xs, ys = campo.exterior.xy
        ax.plot(xs, ys, color="#ffd21e", lw=2.6, zorder=5)
    # salidas
    for s in datos["salidas"]:
        ax.scatter([s["lon"]], [s["lat"]], s=120, facecolor="#ff3b30",
                   edgecolor="white", lw=1.5, zorder=6)
        ax.annotate(f"Salida {s['rumbo']}\n(~{s['acc_ha']} ha)", (s["lon"], s["lat"]),
                    fontsize=8.5, fontweight="bold", color="#b00", ha="center",
                    xytext=(s["lon"], s["lat"]),
                    bbox=dict(boxstyle="round,pad=0.25", fc="white", ec="#b00", alpha=0.9), zorder=7)
    # Encuadrar SIEMPRE en el campo (con un margen), no en el DEM:
    # así el campo queda centrado aunque el DEM sea más grande o esté corrido.
    if campo is not None:
        fx0, fy0, fx1, fy1 = campo.bounds
        pad = max(0.18 * (fx1 - fx0), 0.18 * (fy1 - fy0))
        ax.set_xlim(max(bounds[0], fx0 - pad), min(bounds[2], fx1 + pad))
        ax.set_ylim(max(bounds[1], fy0 - pad), min(bounds[3], fy1 + pad))
    else:
        ax.set_xlim(bounds[0], bounds[2]); ax.set_ylim(bounds[1], bounds[3])
    ax.set_title(f"Escurrimiento del agua — {nombre}", fontsize=12, fontweight="bold", pad=8)
    ax.set_xlabel("Longitud", fontsize=9); ax.set_ylabel("Latitud", fontsize=9)
    ax.tick_params(labelsize=8)
    leg = [Line2D([0], [0], color="#07306f", lw=5, label="Cauce principal (sale el agua)"),
           Line2D([0], [0], color="#1565d8", lw=3.5, label="Escurrimiento concentrado"),
           Line2D([0], [0], color="#5ab0ff", lw=2.5, label="Escurrimiento menor"),
           Patch(facecolor="#1b6b4a", label="Zonas bajas / anegables"),
           Patch(facecolor="#a9815a", label="Zonas altas / lomas"),
           Line2D([0], [0], marker="o", color="w", markerfacecolor="#ff3b30",
                  markersize=10, label="Punto de salida del agua")]
    ax.legend(handles=leg, loc="lower left", fontsize=8, framealpha=0.93, facecolor="white")
    plt.tight_layout()
    fig.savefig(png_path, dpi=150, bbox_inches="tight")
    plt.close(fig)


def generar_kmz(acc, dem, field, bounds, campo, nombre, kmz_path):
    from scipy.ndimage import binary_dilation, zoom
    from PIL import Image
    H, W = acc.shape
    minor = binary_dilation(field & (acc > 80))
    major = binary_dilation(field & (acc > 800))
    trunk = binary_dilation(field & (acc > 4000), iterations=2)
    inside = dem.copy().astype(float); inside[~field] = np.nan
    p = np.nanpercentile(inside, [15, 40])
    muy_bajo = field & (dem < p[0]); bajo = field & (dem >= p[0]) & (dem < p[1])
    f = 4
    up = lambda m: zoom(m.astype(float), f, order=0) > 0.5
    mi, ma, tr, mb, bj = up(minor), up(major), up(trunk), up(muy_bajo), up(bajo)
    img = np.zeros((mi.shape[0], mi.shape[1], 4), dtype=np.float32)
    hx = lambda s: tuple(int(s[i:i+2], 16) / 255 for i in (0, 2, 4))
    def put(mask, rgb, a):
        for k in range(3):
            img[..., k] = np.where(mask, rgb[k], img[..., k])
        img[..., 3] = np.where(mask, a, img[..., 3])
    put(bj, hx("3ad1c8"), 0.25); put(mb, hx("2aa0ff"), 0.33)
    put(mi, hx("5ab0ff"), 0.75); put(ma, hx("1565d8"), 0.9); put(tr, hx("07306f"), 1.0)
    carpeta = os.path.dirname(kmz_path)
    png = os.path.join(carpeta, "_overlay.png")
    Image.fromarray((img * 255).astype(np.uint8), "RGBA").save(png)
    coords = " ".join(f"{x},{y},0" for x, y in campo.exterior.coords)
    kml = f'''<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document>
<name>Escurrimiento - {nombre}</name>
<GroundOverlay><name>Escurrimiento</name><color>ddffffff</color>
<Icon><href>_overlay.png</href></Icon>
<LatLonBox><north>{bounds[3]}</north><south>{bounds[1]}</south><east>{bounds[2]}</east><west>{bounds[0]}</west></LatLonBox>
</GroundOverlay>
<Placemark><name>Contorno {nombre}</name>
<Style><LineStyle><color>ff1ed2ff</color><width>3</width></LineStyle><PolyStyle><fill>0</fill></PolyStyle></Style>
<Polygon><outerBoundaryIs><LinearRing><coordinates>{coords}</coordinates></LinearRing></outerBoundaryIs></Polygon>
</Placemark></Document></kml>'''
    kml_path = os.path.join(carpeta, "_doc.kml")
    with open(kml_path, "w", encoding="utf-8") as fo:
        fo.write(kml)
    with zipfile.ZipFile(kmz_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(kml_path, "doc.kml"); z.write(png, "_overlay.png")
    os.remove(kml_path); os.remove(png)


# --- Paleta del informe ---
_PRIM = (15, 42, 74)      # azul petróleo oscuro
_ACC = (21, 101, 216)     # azul
_LIGHT = (244, 247, 251)  # fondo tarjetas
_BORD = (214, 224, 238)
_GREEN = (22, 107, 74)
_BROWN = (158, 110, 70)
_MUT = (130, 138, 150)
_TXT = (38, 42, 48)
_WBG = (255, 248, 234)
_WBD = (239, 211, 150)
_WTX = (120, 86, 10)


def _L(s):
    """Texto seguro para fuentes core (latin-1)."""
    for a, b in (("→", "->"), ("—", "-"), ("–", "-"), ("•", "-"),
                 ("≈", "~"), ("✓", ""), ("’", "'"), ("“", '"'), ("”", '"')):
        s = s.replace(a, b)
    return s.encode("latin-1", "replace").decode("latin-1")


def _rrect(pdf, x, y, w, h, r, style):
    try:
        pdf.rounded_rect(x, y, w, h, r, style=style)
    except Exception:
        pdf.rect(x, y, w, h, style)


def generar_pdf(png_path, nombre, datos, pdf_path):
    try:
        from fpdf import FPDF
    except ImportError:
        import subprocess
        print("   · Instalando librería de PDF (fpdf2) por única vez ...")
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "fpdf2"], check=False)
        from fpdf import FPDF
    from PIL import Image
    import textwrap
    diag, reco = narrar(nombre, datos)

    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(False)

    # ===================== PÁGINA 1 — MAPA =====================
    pdf.add_page()
    pdf.set_fill_color(*_PRIM); pdf.rect(0, 0, 210, 27, "F")
    pdf.set_text_color(255, 255, 255)
    pdf.set_xy(14, 6); pdf.set_font("Helvetica", "B", 19)
    pdf.cell(182, 9, _L("Análisis de Escurrimiento del Agua"), 0, 1)
    pdf.set_xy(14, 16); pdf.set_font("Helvetica", "", 10.5)
    pdf.cell(182, 6, _L(f"{nombre}   ·   {datos['ha_campo']} ha   ·   "
                        f"Modelo de elevación Copernicus GLO-30 (30 m)"), 0, 1)

    # mapa (ajustado al ancho, dejando lugar para las tarjetas)
    iw, ih = Image.open(png_path).size
    w = 186; h = w * ih / iw
    if h > 206:
        h = 206; w = h * iw / ih
    x = (210 - w) / 2
    pdf.image(png_path, x=x, y=31, w=w, h=h)

    # tarjetas de datos
    cy = 31 + h + 6
    if cy > 268:
        cy = 268
    tarjetas = [("Superficie", f"{datos['ha_campo']} ha", _ACC),
                ("Desnivel total", f"{datos['desnivel']} m", _ACC),
                ("Zonas anegables", f"{datos['ha_anegable']} ha", _GREEN),
                ("Lomas altas", f"{datos['ha_alto']} ha", _BROWN)]
    gap = 4; cw = (186 - 3 * gap) / 4; cx = 12
    for lab, val, col in tarjetas:
        pdf.set_fill_color(*_LIGHT); pdf.set_draw_color(*_BORD)
        _rrect(pdf, cx, cy, cw, 22, 2.5, "DF")
        pdf.set_text_color(*col); pdf.set_font("Helvetica", "B", 15)
        pdf.set_xy(cx, cy + 4.5); pdf.cell(cw, 7, _L(val), 0, 0, "C")
        pdf.set_text_color(*_MUT); pdf.set_font("Helvetica", "", 8)
        pdf.set_xy(cx, cy + 13); pdf.multi_cell(cw, 4, _L(lab), 0, "C")
        cx += cw + gap

    # ===================== PÁGINA 2 — ANÁLISIS =====================
    pdf.add_page()
    pdf.set_fill_color(*_PRIM); pdf.rect(0, 0, 210, 15, "F")
    pdf.set_text_color(255, 255, 255); pdf.set_xy(14, 4); pdf.set_font("Helvetica", "B", 12)
    pdf.cell(182, 7, _L(f"{nombre}  -  Diagnóstico y recomendaciones"), 0, 1)

    y = 24
    y = _seccion(pdf, "Diagnóstico", diag, y, _ACC)
    y += 5
    y = _seccion(pdf, "Recomendaciones de manejo", reco, y, _GREEN)
    y += 5

    # caja de límites
    txt = _L("Límite del estudio: resolución 30 m. El patrón general del agua (hacia dónde "
             "escurre, dónde se junta) es confiable; las líneas finas dentro del lote son "
             "orientativas. Para decisiones de obra conviene rehacerlo con un DEM de 5 m (IGN MDE-Ar).")
    lineas = textwrap.wrap(txt, 96)
    bh = len(lineas) * 4.6 + 7
    pdf.set_fill_color(*_WBG); pdf.set_draw_color(*_WBD)
    _rrect(pdf, 12, y, 186, bh, 2.5, "DF")
    pdf.set_text_color(*_WTX); pdf.set_font("Helvetica", "", 9)
    pdf.set_xy(16, y + 3.5); pdf.multi_cell(178, 4.6, txt, 0, "L")

    # pie
    pdf.set_y(-15); pdf.set_text_color(*_MUT); pdf.set_font("Helvetica", "", 7.5)
    pdf.multi_cell(0, 4, _L("Fuente de elevación: Copernicus GLO-30 (ESA/Airbus), repositorio abierto "
                            "de AWS. Método: dirección de flujo D8 + acumulación de flujo (pysheds). "
                            "Contorno del campo provisto por el usuario."), 0, "L")
    pdf.output(pdf_path)


def _seccion(pdf, titulo, items, y, color):
    pdf.set_fill_color(*color); _rrect(pdf, 12, y, 186, 8.5, 1.5, "F")
    pdf.set_text_color(255, 255, 255); pdf.set_font("Helvetica", "B", 11)
    pdf.set_xy(16, y + 1.4); pdf.cell(178, 6, _L(titulo), 0, 1)
    y += 12
    pdf.set_text_color(*_TXT); pdf.set_font("Helvetica", "", 10)
    for it in items:
        pdf.set_fill_color(*color); pdf.rect(14, y + 1.6, 2, 2, "F")
        pdf.set_xy(19, y); pdf.multi_cell(179, 5, _L(it), 0, "L")
        y = pdf.get_y() + 3
    return y


# ----------------------------------------------------------------------------
# Principal
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Análisis de escurrimiento de un campo desde su KML/KMZ.")
    ap.add_argument("kml", help="Archivo KML o KMZ con el contorno del campo.")
    ap.add_argument("--buffer", type=float, default=3.0, help="Margen alrededor del campo, en km (def. 3).")
    ap.add_argument("--salida", default=None, help="Carpeta de salida (def.: junto al KML).")
    ap.add_argument("--dem", default=None, help="Usar un DEM propio (.tif) en vez de bajar Copernicus 30 m.")
    args = ap.parse_args()

    if not os.path.exists(args.kml):
        raise SystemExit(f"No existe el archivo: {args.kml}")
    carpeta = args.salida or os.path.dirname(os.path.abspath(args.kml))
    os.makedirs(carpeta, exist_ok=True)

    print("\nLeyendo campos del archivo ...")
    campos = leer_contornos(args.kml)
    n = len(campos)
    if n == 1:
        print(f"Se encontró 1 campo: {campos[0]['nombre']}")
    else:
        print(f"Se encontraron {n} campos: " + ", ".join(c["nombre"] for c in campos))

    ok, fallidos = [], []
    with tempfile.TemporaryDirectory() as tmp:  # tiles compartidos entre campos
        for i, c in enumerate(campos, 1):
            nombre = c["nombre"]; campo = c["poly"]
            print(f"\n──────────  [{i}/{n}]  {nombre}  ──────────")
            try:
                procesar_campo(campo, nombre, carpeta, args, tmp)
                ok.append(nombre)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                print(f"   ⚠️  No se pudo procesar '{nombre}': {e}")
                print("       (sigo con el próximo campo)")
                fallidos.append(nombre)

    print("\n============================================")
    print(f"✅ Terminado. {len(ok)} de {n} campos procesados.")
    print(f"   Resultados en: {carpeta}")
    if fallidos:
        print("   ⚠️  No salieron: " + ", ".join(fallidos))
    print("============================================")


def procesar_campo(campo, nombre, carpeta, args, tmp):
    minx, miny, maxx, maxy = campo.bounds
    km = args.buffer
    dlat = km / 111.0
    dlon = km / (111.32 * math.cos(math.radians((miny + maxy) / 2)))
    bbox = (minx - dlon, miny - dlat, maxx + dlon, maxy + dlat)
    area_ha = round(campo.area * (111000 ** 2) * math.cos(math.radians((miny + maxy) / 2)) / 10000)
    print(f"   Contorno ~{area_ha} ha. Zona con {km} km de margen.")

    if args.dem:
        print(f"   Usando DEM propio: {args.dem}")
        dem_path = recortar_dem_propio(args.dem, bbox, tmp)
    else:
        print("   Bajando / reutilizando DEM Copernicus GLO-30 (AWS) ...")
        dem_path = descargar_dem(bbox, tmp)
    print("   Análisis hidrológico (flujo D8) ...")
    acc, dem, bounds = correr_flujo(dem_path)
    field = rasterizar_campo(campo, acc.shape, bounds)
    if field.sum() == 0:
        raise RuntimeError("el contorno no cayó dentro del DEM descargado")
    datos, mb, bj, al = analizar(acc, dem, field, bounds)

    print("   Generando mapa, KMZ e informe PDF ...")
    png = os.path.join(carpeta, f"Escurrimiento_{nombre}.png")
    kmz = os.path.join(carpeta, f"Escurrimiento_{nombre}.kmz")
    pdf = os.path.join(carpeta, f"Informe_Escurrimiento_{nombre}.pdf")
    generar_mapa(acc, dem, field, bounds, nombre, datos, png, campo=campo)
    generar_kmz(acc, dem, field, bounds, campo, nombre, kmz)
    generar_pdf(png, nombre, datos, pdf)
    print(f"   ✔ {nombre}: ~{datos['ha_campo']} ha | desnivel {datos['desnivel']} m | "
          f"anegable ~{datos['ha_anegable']} ha | "
          + (", ".join(f"salida {s['rumbo']} ~{s['acc_ha']}ha" for s in datos['salidas']) or "sin cauces marcados"))
    return {"nombre": nombre, "datos": datos, "png": png, "kmz": kmz, "pdf": pdf}


def recortar_dem_propio(dem_path, bbox, tmp):
    import rasterio
    from rasterio.windows import from_bounds as win_from_bounds
    W, S, E, N = bbox
    with rasterio.open(dem_path) as ds:
        b = ds.bounds
        # Intersecar el recuadro pedido con lo que el DEM realmente cubre,
        # así nunca se pide fuera de rango (evita recortes erróneos).
        W2, S2 = max(W, b.left), max(S, b.bottom)
        E2, N2 = min(E, b.right), min(N, b.top)
        if E2 <= W2 or N2 <= S2:
            data = ds.read(1); transform = ds.transform
        else:
            win = win_from_bounds(W2, S2, E2, N2, transform=ds.transform)
            data = ds.read(1, window=win)
            transform = ds.window_transform(win)
        meta = ds.meta.copy()
        meta.update({"height": data.shape[0], "width": data.shape[1],
                     "transform": transform, "count": 1})
    salida = os.path.join(tmp, "dem_propio.tif")
    with rasterio.open(salida, "w", **meta) as dst:
        dst.write(data, 1)
    return salida


if __name__ == "__main__":
    main()
