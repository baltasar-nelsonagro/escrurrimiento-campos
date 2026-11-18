# -*- coding: utf-8 -*-
"""
Web de Análisis de Escurrimiento de Campos (Streamlit).
Subís un KML/KMZ (uno o varios campos) y devuelve mapa + KMZ + PDF de cada uno.

Motor: analisis_escurrimiento.py (el mismo de la versión de escritorio).
Login: usuario/contraseña, configurados en los "Secrets" de la app.
"""
import os
import io
import tempfile
import zipfile
import traceback
from types import SimpleNamespace

import streamlit as st

import analisis_escurrimiento as motor

st.set_page_config(page_title="Escurrimiento de Campos", page_icon="💧", layout="wide")


# ----------------------------------------------------------------------------
# Login con usuario / contraseña
# ----------------------------------------------------------------------------
def _cred(nombre, defecto):
    try:
        return st.secrets.get(nombre, defecto)
    except Exception:
        return os.environ.get(nombre, defecto)


def login():
    if st.session_state.get("autenticado"):
        return True

    st.markdown("## 💧 Análisis de Escurrimiento de Campos")
    st.caption("Herramienta interna — Nelson Agro / San Marcos")
    st.write("Ingresá con el usuario y la contraseña de la empresa.")

    with st.form("login"):
        usuario = st.text_input("Usuario")
        clave = st.text_input("Contraseña", type="password")
        entrar = st.form_submit_button("Entrar")
    if entrar:
        if usuario == _cred("APP_USUARIO", "nelson") and clave == _cred("APP_CLAVE", "sanmarcos"):
            st.session_state["autenticado"] = True
            st.rerun()
        else:
            st.error("Usuario o contraseña incorrectos.")
    return False


# ----------------------------------------------------------------------------
# Procesamiento (sin Streamlit, para poder testearlo aparte)
# ----------------------------------------------------------------------------
def procesar_kml(ruta, buffer_km, progreso=None):
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
                r = motor.procesar_campo(c["poly"], nombre, salida_dir, args, tmp)
                resultados.append(r)
            except Exception as e:
                traceback.print_exc()
                fallidos.append((nombre, str(e)))
    if progreso:
        progreso(1.0, "Armando la descarga…")

    # ZIP con todo en memoria
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for r in resultados:
            for k in ("png", "kmz", "pdf"):
                if os.path.exists(r[k]):
                    z.write(r[k], os.path.basename(r[k]))
    buf.seek(0)
    return resultados, fallidos, buf.getvalue()


# ----------------------------------------------------------------------------
# App
# ----------------------------------------------------------------------------
def main():
    st.markdown("## 💧 Análisis de Escurrimiento de Campos")
    st.caption("Subí el KML/KMZ con el contorno de tu campo (lo dibujás en Google Earth). "
               "Podés poner varios campos en el mismo archivo: se procesan todos.")

    col_in, col_out = st.columns([1, 2])
    with col_in:
        archivo = st.file_uploader("Contorno del campo (KML o KMZ)", type=["kml", "kmz"])
        buffer_km = st.slider("Margen alrededor del campo (km)", 1, 6, 3)
        correr = st.button("Analizar escurrimiento", type="primary", use_container_width=True)
        if st.button("Salir", use_container_width=True):
            st.session_state["autenticado"] = False
            st.rerun()

    if correr:
        if archivo is None:
            st.warning("Subí primero un archivo KML o KMZ.")
            return
        # guardar el subido en un archivo temporal con su extensión
        ext = ".kmz" if archivo.name.lower().endswith(".kmz") else ".kml"
        tmpf = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
        tmpf.write(archivo.getvalue()); tmpf.close()

        barra = st.progress(0.0, text="Arrancando…")
        def prog(frac, txt):
            barra.progress(min(max(frac, 0.0), 1.0), text=txt)
        try:
            with st.spinner("Procesando (baja el mapa de elevación y calcula)…"):
                resultados, fallidos, zip_bytes = procesar_kml(tmpf.name, buffer_km, prog)
        except Exception as e:
            st.error(f"No se pudo procesar el archivo: {e}")
            return
        finally:
            try:
                os.unlink(tmpf.name)
            except Exception:
                pass
        barra.empty()

        if not resultados:
            st.error("No se pudo procesar ningún campo. " +
                     ("Motivo: " + "; ".join(f"{n_}: {e}" for n_, e in fallidos) if fallidos else ""))
            return

        st.success(f"Listo: {len(resultados)} de {len(resultados) + len(fallidos)} campos procesados.")
        st.download_button("⬇️ Descargar todo (ZIP: mapas + KMZ + PDF)",
                           data=zip_bytes, file_name="Resultados_Escurrimiento.zip",
                           mime="application/zip", type="primary")

        # tabla resumen
        filas = []
        for r in resultados:
            d = r["datos"]
            sal = d["salidas"][0] if d["salidas"] else None
            filas.append({"Campo": r["nombre"], "Sup. (ha)": d["ha_campo"],
                          "Desnivel (m)": d["desnivel"], "Anegable (ha)": d["ha_anegable"],
                          "Salida principal": f"{sal['rumbo']} (~{sal['acc_ha']} ha)" if sal else "—"})
        st.table(filas)
        if fallidos:
            st.warning("No salieron: " + ", ".join(f"{n_} ({e})" for n_, e in fallidos))

        # mapas
        with col_out:
            for r in resultados:
                st.image(r["png"], caption=r["nombre"], use_column_width=True)

        st.caption("Mapa de elevación: Copernicus GLO-30 (30 m). El patrón general del agua es "
                   "confiable; las líneas finas internas son orientativas. Para decisiones de obra "
                   "conviene un DEM de 5 m.")


if __name__ == "__main__" or True:
    if login():
        main()
