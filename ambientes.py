# -*- coding: utf-8 -*-
"""
Mapa de ambientes por NDVI (Sentinel-2, vía Microsoft Planetary Computer, gratis sin cuenta).
Funciona para:
  - campos agrícolas  -> el NDVI refleja vigor del cultivo = zonas de productividad.
  - campos de monte   -> el NDVI refleja densidad del monte = proxy de suelo/agua.
Detecta solo cuál es (por la estacionalidad del NDVI) y ajusta la interpretación.

analizar_ambientes(campo, carpeta) -> dict con png + stats + tipo, o None si no se pudo
(sin imágenes, sin internet, poca memoria, etc.). El llamador DEBE tolerar None.
"""
import os
import math
import numpy as np

STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
# Clases SCL de Sentinel-2 a enmascarar (nubes, sombras, nieve, sin dato)
SCL_MALAS = [0, 1, 3, 8, 9, 10, 11]


def analizar_ambientes(campo, carpeta, max_escenas=12):
    try:
        import pystac_client
        import planetary_computer
        import odc.stac
        import rioxarray  # noqa: F401 (activa el accessor .rio)
    except Exception:
        return None

    try:
        minx, miny, maxx, maxy = campo.bounds
        latm = (miny + maxy) / 2.0
        area_ha = campo.area * (111000 ** 2) * math.cos(math.radians(latm)) / 10000.0

        # Resolución (en grados) según tamaño del campo, para acotar memoria
        metros = 20
        if area_ha > 8000: metros = 40
        if area_ha > 25000: metros = 70
        res_deg = metros / 111320.0

        catalog = pystac_client.Client.open(STAC_URL, modifier=planetary_computer.sign_inplace)
        search = catalog.search(
            collections=["sentinel-2-l2a"],
            bbox=[minx, miny, maxx, maxy],
            datetime="2023-01-01/2026-12-31",
            query={"eo:cloud_cover": {"lt": 15}},
        )
        items = list(search.items())
        if not items:
            return None
        items = sorted(items, key=lambda it: it.properties.get("eo:cloud_cover", 100))[:max_escenas]

        ds = odc.stac.load(
            items, bands=["red", "nir", "SCL"],
            bbox=[minx, miny, maxx, maxy],
            crs="EPSG:4326", resolution=res_deg, groupby="solar_day", chunks={},
        )
        # odc usa nombres de assets; en S2-L2A de PC: red=B04, nir=B08, SCL
        scl = ds["SCL"]
        buena = ~scl.isin(SCL_MALAS)
        red = ds["red"].where(buena).astype("float32")
        nir = ds["nir"].where(buena).astype("float32")
        ndvi = (nir - red) / (nir + red)

        ndvi_med = ndvi.median(dim="time", skipna=True)
        ndvi_std = ndvi.std(dim="time", skipna=True)

        # Recorte al polígono del campo
        ndvi_med = ndvi_med.rio.write_crs("EPSG:4326")
        ndvi_med = ndvi_med.rio.clip([campo.__geo_interface__], crs="EPSG:4326", drop=True)
        arr = np.asarray(ndvi_med.values, dtype="float32")
        # bounds reales del raster recortado
        xs = ndvi_med["longitude"].values if "longitude" in ndvi_med.coords else ndvi_med["x"].values
        ys = ndvi_med["latitude"].values if "latitude" in ndvi_med.coords else ndvi_med["y"].values
        bounds = (float(xs.min()), float(ys.min()), float(xs.max()), float(ys.max()))

        dentro = np.isfinite(arr)
        if dentro.sum() < 25:   # muy poca cobertura -> no confiable
            return None

        # std temporal medio dentro del campo (para ag vs monte)
        std_med = ndvi_std.rio.write_crs("EPSG:4326").rio.clip(
            [campo.__geo_interface__], crs="EPSG:4326", drop=True)
        std_val = float(np.nanmean(np.asarray(std_med.values, dtype="float32")))
        ndvi_prom = float(np.nanmean(arr[dentro]))

        tipo = "agrícola" if std_val >= 0.14 else "monte / natural"

        stats, png = _zonificar_y_mapear(arr, dentro, bounds, campo, carpeta, tipo)
        return {"png": png, "tipo": tipo, "ndvi_prom": round(ndvi_prom, 2),
                "std_temporal": round(std_val, 3), "n_escenas": len(items),
                "zonas": stats}
    except Exception:
        return None


def _zonificar_y_mapear(arr, dentro, bounds, campo, carpeta, tipo):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import ListedColormap
    import numpy as np

    vals = arr[dentro]
    # 3 ambientes por terciles del NDVI
    q1, q2 = np.nanpercentile(vals, [33, 66])
    zonas = np.full(arr.shape, np.nan)
    zonas[dentro & (arr <= q1)] = 0
    zonas[dentro & (arr > q1) & (arr <= q2)] = 1
    zonas[dentro & (arr > q2)] = 2
    cell = (bounds[2] - bounds[0]) / arr.shape[1] * 111320.0 * \
           (bounds[3] - bounds[1]) / arr.shape[0] * 111320.0 / 10000.0
    stats = []
    etiquetas = ["Bajo (menor vigor)", "Medio", "Alto (mayor vigor)"]
    for z in (0, 1, 2):
        n = int(np.nansum(zonas == z))
        stats.append({"zona": etiquetas[z], "ha": round(n * abs(cell)),
                      "pct": round(100 * n / max(dentro.sum(), 1))})

    extent = [bounds[0], bounds[2], bounds[1], bounds[3]]
    cmap = ListedColormap(["#c44536", "#e9c46a", "#2a9d4a"])  # rojo-amarillo-verde
    fig, ax = plt.subplots(figsize=(10, 11), dpi=150)
    ax.imshow(np.ma.masked_invalid(zonas), extent=extent, origin="upper",
              cmap=cmap, vmin=0, vmax=2, interpolation="nearest")
    xs, ys = campo.exterior.xy
    ax.plot(xs, ys, color="#1a1a1a", lw=2.2, zorder=5)
    ax.set_title(f"Ambientes por vigor de vegetación (NDVI) — {tipo}", fontsize=12, fontweight="bold", pad=8)
    ax.set_xlabel("Longitud", fontsize=9); ax.set_ylabel("Latitud", fontsize=9)
    ax.tick_params(labelsize=8)
    from matplotlib.patches import Patch
    leg = [Patch(facecolor="#2a9d4a", label="Ambiente alto (mayor vigor)"),
           Patch(facecolor="#e9c46a", label="Ambiente medio"),
           Patch(facecolor="#c44536", label="Ambiente bajo (menor vigor)")]
    ax.legend(handles=leg, loc="lower left", fontsize=8.5, framealpha=0.93, facecolor="white")
    plt.tight_layout()
    png = os.path.join(carpeta, "ambientes.png")
    fig.savefig(png, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return stats, png
