# -*- coding: utf-8 -*-
"""
Análisis de calidad de suelo de un campo, usando SoilGrids (ISRIC, global, 250 m).
Consulta las propiedades en el punto central del campo y las interpreta.

Devuelve un dict con los valores (0-30 cm) e interpretación, o None si no se
pudo consultar (sin internet, servicio caído, etc.) — el llamador debe tolerar None.
"""
import json
import ssl
import urllib.request
import urllib.error

SOILGRIDS = "https://rest.isric.org/soilgrids/v2.0/properties/query"
PROPS = ["clay", "sand", "silt", "phh2o", "soc", "cec", "nitrogen", "bdod"]
DEPTHS = ["0-5cm", "5-15cm", "15-30cm"]
THICK = {"0-5cm": 5, "5-15cm": 10, "15-30cm": 15}  # para promedio ponderado 0-30


def _ctx():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _consultar(lon, lat, timeout=25, intentos=3):
    q = f"{SOILGRIDS}?lon={lon:.5f}&lat={lat:.5f}"
    for p in PROPS:
        q += f"&property={p}"
    for d in DEPTHS:
        q += f"&depth={d}"
    q += "&value=mean"
    ctx = _ctx()
    ultimo = None
    for _ in range(intentos):
        try:
            req = urllib.request.Request(q, headers={"User-Agent": "escurrimiento-suelo/1.0"})
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            ultimo = str(e)
    raise RuntimeError(ultimo or "no se pudo consultar SoilGrids")


def _prom_0_30(layer):
    """Promedio ponderado 0-30 cm de una capa de SoilGrids."""
    tot_v = tot_t = 0.0
    for dep in layer.get("depths", []):
        lab = dep.get("label")
        val = (dep.get("values") or {}).get("mean")
        if lab in THICK and val is not None:
            tot_v += val * THICK[lab]
            tot_t += THICK[lab]
    return (tot_v / tot_t) if tot_t else None


def _clase_textural(arena, limo, arcilla):
    """Clase textural USDA a partir de % arena/limo/arcilla."""
    if None in (arena, limo, arcilla):
        return None
    a, l, ar = arena, limo, arcilla
    if ar >= 40:
        if a >= 45: return "Franco-arcillo-arenoso" if ar < 55 else "Arcillo-arenoso"
        if l >= 40: return "Arcillo-limoso"
        return "Arcilloso"
    if ar >= 27:
        if a >= 45: return "Franco-arcillo-arenoso"
        if l >= 28: return "Franco-arcillo-limoso"
        return "Franco-arcilloso"
    if l >= 50:
        return "Limoso" if ar < 12 and l >= 80 else "Franco-limoso"
    if a >= 85:
        return "Arenoso"
    if a >= 70:
        return "Franco-arenoso"  # (areno-franco aprox.)
    if a >= 43 and ar < 20:
        return "Franco-arenoso"
    return "Franco"


def analizar_suelo(campo_polygon):
    """campo_polygon: shapely Polygon. Devuelve dict o None."""
    try:
        c = campo_polygon.centroid
        lon, lat = float(c.x), float(c.y)
        data = _consultar(lon, lat)
        layers = {l["name"]: l for l in data["properties"]["layers"]}

        def val(name, factor):
            l = layers.get(name)
            if not l:
                return None
            v = _prom_0_30(l)
            return round(v * factor, 1) if v is not None else None

        arcilla = val("clay", 0.1)   # g/kg -> %
        arena = val("sand", 0.1)
        limo = val("silt", 0.1)
        ph = val("phh2o", 0.1)        # pH*10 -> pH
        soc = val("soc", 0.01)        # dg/kg -> %
        cic = val("cec", 0.1)         # mmol(c)/kg -> cmol/kg
        nitro = val("nitrogen", 0.01) # cg/kg -> g/kg aprox
        dens = val("bdod", 0.01)      # cg/cm3 -> g/cm3
        mo = round(soc * 1.724, 1) if soc is not None else None  # materia orgánica

        d = {"lon": round(lon, 5), "lat": round(lat, 5),
             "arcilla": arcilla, "arena": arena, "limo": limo,
             "ph": ph, "mo": mo, "soc": soc, "cic": cic, "densidad": dens,
             "textura": _clase_textural(arena, limo, arcilla)}
        d["interpretacion"] = _interpretar(d)
        return d
    except Exception:
        return None


def _interpretar(d):
    out = []
    tx = d.get("textura")
    arena, arcilla = d.get("arena"), d.get("arcilla")
    if tx:
        if arcilla and arcilla >= 35:
            out.append(f"Textura {tx.lower()}: suelo pesado — alta retención de agua y nutrientes, "
                       f"pero drenaje lento y riesgo de anegamiento/compactación.")
        elif arena and arena >= 70:
            out.append(f"Textura {tx.lower()}: suelo liviano — buen drenaje y laboreo fácil, "
                       f"pero baja retención de agua y nutrientes (se seca rápido).")
        else:
            out.append(f"Textura {tx.lower()}: suelo de comportamiento equilibrado — "
                       f"buena retención de agua con drenaje razonable.")
    ph = d.get("ph")
    if ph is not None:
        if ph < 5.5:
            out.append(f"pH {ph}: fuertemente ácido — puede limitar disponibilidad de fósforo y requerir encalado.")
        elif ph < 6.5:
            out.append(f"pH {ph}: ligeramente ácido — rango favorable para la mayoría de los cultivos y pasturas.")
        elif ph <= 7.3:
            out.append(f"pH {ph}: neutro — óptimo para disponibilidad de nutrientes.")
        elif ph <= 8.2:
            out.append(f"pH {ph}: alcalino — posible presencia de carbonatos; vigilar micronutrientes.")
        else:
            out.append(f"pH {ph}: fuertemente alcalino — posible sodicidad/salinidad; revisar en campo.")
    mo = d.get("mo")
    if mo is not None:
        if mo < 2:
            out.append(f"Materia orgánica {mo}%: baja — fertilidad y estructura limitadas; valorar aportes y rotación.")
        elif mo <= 3.5:
            out.append(f"Materia orgánica {mo}%: media — nivel aceptable, mantenible con buen manejo.")
        else:
            out.append(f"Materia orgánica {mo}%: alta — buena fertilidad y estructura.")
    cic = d.get("cic")
    if cic is not None:
        nivel = "baja" if cic < 12 else ("media" if cic <= 25 else "alta")
        out.append(f"CIC {cic} cmol/kg: capacidad de retener nutrientes {nivel}.")
    return out
