# -*- coding: utf-8 -*-
"""
Web de Análisis de Escurrimiento de Campos (Streamlit) — sin login, diseño profesional.
Subís un KML/KMZ (uno o varios campos) y devuelve mapa + KMZ + PDF de cada uno.
Motor: analisis_escurrimiento.py
"""
import os
import io
import tempfile
import zipfile
import traceback
from types import SimpleNamespace

import streamlit as st

import analisis_escurrimiento as motor
import suelo as suelo_mod
import ambientes as ambientes_mod

st.set_page_config(page_title="Escurrimiento y Suelo · Nelson Agro",
                   page_icon="💧", layout="wide")

BORDO = "#6B1F2A"
BORDO2 = "#8A2A38"
MOSTAZA = "#F2C500"

CSS = f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&display=swap');

#MainMenu, header, footer {{visibility: hidden;}}
[data-testid="stToolbar"], [data-testid="stDecoration"], [data-testid="stStatusWidget"] {{display: none;}}
html, body, [class*="css"], .stMarkdown, p, div, span, label {{ font-family: 'Inter', sans-serif; }}
.stApp {{ background: #FAF7F3; }}
.block-container {{ padding-top: 1.5rem; padding-bottom: 2rem; max-width: 1180px; }}

/* ---------- HERO ---------- */
.hero {{
  position: relative; overflow: hidden;
  background: linear-gradient(135deg, {BORDO} 0%, {BORDO2} 55%, #5c1a24 100%);
  color: #fff; padding: 40px 44px 46px; border-radius: 20px;
  box-shadow: 0 12px 34px rgba(107,31,42,.26);
  margin-bottom: 30px;
}}
.hero::after {{
  content:""; position:absolute; right:-60px; top:-60px;
  width: 260px; height: 260px; border-radius: 50%;
  background: radial-gradient(circle at center, rgba(242,197,0,.22), transparent 70%);
}}
.hero .kicker {{
  display:inline-block; background: rgba(242,197,0,.18); color: {MOSTAZA};
  font-weight:700; font-size:.72rem; letter-spacing:.14em; text-transform:uppercase;
  padding:5px 12px; border-radius: 999px; margin-bottom: 14px; border:1px solid rgba(242,197,0,.35);
}}
.hero h1 {{ margin:0; font-size: 2.5rem; font-weight: 900; line-height:1.08; letter-spacing:-.5px; }}
.hero p  {{ margin:14px 0 0; font-size: 1.06rem; opacity:.94; max-width: 640px; line-height:1.5; }}
.hero .bar {{ height:5px; width: 90px; background:{MOSTAZA}; border-radius: 3px; margin-top: 22px; }}

/* ---------- STEP CARDS ---------- */
.steps {{ display:flex; gap:16px; margin: 0 0 30px; flex-wrap: wrap; }}
.step {{
  flex:1; min-width: 210px; background:#fff; border:1px solid #ECE3D8; border-radius:16px;
  padding:20px 22px; box-shadow:0 2px 12px rgba(0,0,0,.04);
}}
.step .n {{
  display:inline-flex; align-items:center; justify-content:center;
  width:34px; height:34px; border-radius:10px; background:{BORDO}; color:#fff;
  font-weight:800; font-size:1rem; margin-bottom:12px;
}}
.step h4 {{ margin:0 0 4px; color:{BORDO}; font-size:1.02rem; font-weight:800; }}
.step p  {{ margin:0; color:#6b6258; font-size:.9rem; line-height:1.45; }}

/* ---------- PANELS ---------- */
.panel-title {{ color:{BORDO}; font-weight:800; font-size:1.15rem; margin:4px 0 2px; }}
[data-testid="stFileUploader"] {{
  background:#fff; border:1.5px dashed #d8c9b8; border-radius:14px; padding:10px 14px;
}}
.stButton button[kind="primary"] {{
  background:{BORDO}; border:none; font-weight:800; border-radius:12px; padding:.6rem 1rem;
  font-size:1rem; box-shadow:0 4px 14px rgba(107,31,42,.22);
}}
.stButton button[kind="primary"]:hover {{ background:{BORDO2}; }}
.stDownloadButton button {{
  background:{MOSTAZA}; color:{BORDO}; border:none; font-weight:900; border-radius:12px; padding:.6rem 1rem;
}}
.stDownloadButton button:hover {{ background:#ffd531; }}

.placeholder {{
  background:#fff; border:1px solid #ECE3D8; border-radius:16px; padding:28px 30px;
  color:#6b6258; box-shadow:0 2px 12px rgba(0,0,0,.04);
}}
.placeholder b {{ color:{BORDO}; }}
.foot {{ color:#a79a8b; font-size:.8rem; margin-top:34px; text-align:center;
  border-top:1px solid #ECE3D8; padding-top:16px; }}
h2,h3,h5 {{ color:{BORDO}; }}
</style>
"""


def hero():
    st.markdown(CSS, unsafe_allow_html=True)
    st.markdown(f"""
    <div class="hero">
      <span class="kicker">Nelson Agro · San Marcos</span>
      <h1>Análisis de Escurrimiento<br>y Suelo de Campos</h1>
      <p>Subí el contorno de tu campo y obtené un informe único: por dónde corre y se junta el agua,
         la calidad del suelo (textura, pH, materia orgánica) y un mapa de ambientes (variación interna),
         con recomendaciones de manejo. Sin instalar nada.</p>
      <div class="bar"></div>
    </div>""", unsafe_allow_html=True)


def pasos():
    st.markdown("""
    <div class="steps">
      <div class="step"><div class="n">1</div><h4>Dibujá el campo</h4>
        <p>En Google Earth, marcá el contorno con la herramienta de polígono y exportalo como KML.</p></div>
      <div class="step"><div class="n">2</div><h4>Subí el archivo</h4>
        <p>Cargá el KML/KMZ acá. Podés poner varios campos en el mismo archivo.</p></div>
      <div class="step"><div class="n">3</div><h4>Descargá el informe</h4>
        <p>Un PDF unificado: escurrimiento del agua, calidad de suelo y mapa de ambientes, con recomendaciones.</p></div>
    </div>""", unsafe_allow_html=True)


# ----------------------------------------------------------------------------
# Procesamiento (sin Streamlit, para testear aparte)
# ----------------------------------------------------------------------------
def procesar_kml(ruta, buffer_km, incluir_suelo=False, progreso=None):
    campos = motor.leer_contornos(ruta)
    n = len(campos)
    args = SimpleNamespace(buffer=float(buffer_km), dem=None, salida=None)
    salida_dir = tempfile.mkdtemp(prefix="escur_")
    resultados, fallidos = [], []
    with tempfile.TemporaryDirectory() as tmp:
        for i, c in enumerate(campos, 1):
            nombre = c["nombre"]
            if progreso:
                progreso(i / (n + 1), f"[{i}/{n}] Procesando {nombre} …")
            try:
                s = None
                amb = None
                if incluir_suelo:
                    if progreso:
                        progreso(i / (n + 1), f"[{i}/{n}] {nombre}: consultando suelo …")
                    s = suelo_mod.analizar_suelo(c["poly"])
                    if progreso:
                        progreso(i / (n + 1), f"[{i}/{n}] {nombre}: mapa de ambientes (satélite) …")
                    a = ambientes_mod.analizar_ambientes(c["poly"], salida_dir)
                    amb = a if a else "no_disponible"
                r = motor.procesar_campo(c["poly"], nombre, salida_dir, args, tmp, suelo=s, ambientes=amb)
                if not isinstance(r, dict):
                    raise RuntimeError("el motor no devolvió el resultado (actualizá analisis_escurrimiento.py)")
                resultados.append(r)
            except Exception as e:
                traceback.print_exc()
                fallidos.append((nombre, str(e)))
    if progreso:
        progreso(1.0, "Preparando el informe…")
    return resultados, fallidos


# ----------------------------------------------------------------------------
# App
# ----------------------------------------------------------------------------
def main():
    hero()
    pasos()

    col_in, col_out = st.columns([1, 1.5], gap="large")
    with col_in:
        st.markdown('<div class="panel-title">Cargá tu campo</div>', unsafe_allow_html=True)
        archivo = st.file_uploader("Contorno del campo (KML o KMZ)", type=["kml", "kmz"],
                                   label_visibility="collapsed")
        buffer_km = st.slider("Margen alrededor del campo (km)", 1, 6, 3)
        correr = st.button("Analizar", type="primary", use_container_width=True)
        st.caption("El informe incluye escurrimiento del agua, calidad de suelo y mapa de ambientes.")

    with col_out:
        if not correr:
            st.markdown(
                '<div class="placeholder">👈 Cargá un archivo y apretá '
                '<b>Analizar</b>.<br><br>Por cada campo vas a obtener un '
                '<b>informe PDF unificado</b>: el <b>mapa del agua</b> (dónde conviene una represa, '
                'dónde el casco, qué zonas se anegan), la <b>calidad de suelo</b> '
                '(textura, pH, materia orgánica) y un <b>mapa de ambientes</b> que muestra la '
                'variación interna del campo.</div>',
                unsafe_allow_html=True)

    if correr:
        if archivo is None:
            st.warning("Subí primero un archivo KML o KMZ.")
            _foot(); return
        ext = ".kmz" if archivo.name.lower().endswith(".kmz") else ".kml"
        tmpf = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
        tmpf.write(archivo.getvalue()); tmpf.close()

        barra = st.progress(0.0, text="Arrancando…")
        def prog(frac, txt):
            barra.progress(min(max(frac, 0.0), 1.0), text=txt)
        try:
            with st.spinner("Procesando (baja el mapa de elevación y calcula)…"):
                resultados, fallidos = procesar_kml(tmpf.name, buffer_km, True, prog)
        except Exception as e:
            st.error(f"No se pudo procesar el archivo: {e}")
            with st.expander("Ver detalle técnico del error"):
                st.code(traceback.format_exc())
            _foot(); return
        finally:
            try:
                os.unlink(tmpf.name)
            except Exception:
                pass
        barra.empty()

        if not resultados:
            st.error("No se pudo procesar ningún campo. " +
                     ("Motivo: " + "; ".join(f"{n_}: {e}" for n_, e in fallidos) if fallidos else ""))
            _foot(); return

        st.success(f"Listo: {len(resultados)} de {len(resultados) + len(fallidos)} campos procesados.")

        # Solo el informe PDF
        pdfs = [(r.get("nombre", "Campo"), r.get("pdf")) for r in resultados
                if r.get("pdf") and os.path.exists(r.get("pdf"))]
        if len(pdfs) == 1:
            nombre, ruta = pdfs[0]
            with open(ruta, "rb") as fh:
                st.download_button(f"⬇️  Descargar informe PDF — {nombre}",
                                   data=fh.read(), file_name=os.path.basename(ruta),
                                   mime="application/pdf", use_container_width=True)
        elif len(pdfs) > 1:
            # varios campos: un ZIP solo con los PDF
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for nombre, ruta in pdfs:
                    z.write(ruta, os.path.basename(ruta))
            buf.seek(0)
            st.download_button("⬇️  Descargar informes PDF (ZIP)",
                               data=buf.getvalue(), file_name="Informes_Escurrimiento.zip",
                               mime="application/zip", use_container_width=True)

        if fallidos:
            st.warning("No salieron: " + ", ".join(f"{n_} ({e})" for n_, e in fallidos))
    _foot()


def _foot():
    st.markdown('<div class="foot">Nelson Agro / San Marcos · Datos: Copernicus GLO-30 (ESA/Airbus) · '
                'Método: dirección de flujo D8 + acumulación de flujo (pysheds)</div>',
                unsafe_allow_html=True)


main()
