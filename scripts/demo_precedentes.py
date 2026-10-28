"""
Datos de ejemplo para probar Precedentes y Machotes (Fase 4) en un despacho de prueba.

Crea asuntos concluidos con su resultado, sentencias y lecciones, completa datos de los asuntos
activos (solo campos vacíos) y sube tres machotes Word a la biblioteca. Se puede correr varias
veces: no duplica nada.

Uso:
    venv/bin/python scripts/demo_precedentes.py --tenant bufete-ramirez
"""
import argparse
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docx import Document  # noqa: E402
from docx.enum.text import WD_ALIGN_PARAGRAPH  # noqa: E402
from docx.shared import Pt  # noqa: E402
from flask import g  # noqa: E402
from werkzeug.datastructures import FileStorage  # noqa: E402

from app import create_app  # noqa: E402
from modules import boveda  # noqa: E402
from modules.insights import casos  # noqa: E402

FAM, MER, AMP = ("Juzgado Tercero de lo Familiar de la CDMX", "Juzgado Décimo de lo Civil de la CDMX",
                 "Juzgado Segundo de Distrito en Materia Administrativa en la CDMX")

CONCLUIDOS = [
    dict(numero_expediente="845/2023", titulo="Divorcio incausado con convenio de custodia — Ortega Luna",
         tipo_asunto="familiar", sub_tipo="divorcio", juzgado=FAM, contrario_despacho="Bufete Salgado y Asociados",
         descripcion="Divorcio sin causa con dos hijos menores; desacuerdo sobre custodia, régimen de visitas y pensión.",
         fecha_inicio="2023-03-10", fecha_cierre="2023-11-22", resultado="favorable",
         resultado_notas="Una propuesta de convenio con régimen de visitas detallado y pensión calculada sobre ingresos "
                         "comprobables evitó la controversia. Ofrecer desde el inicio la pericial en trabajo social "
                         "aceleró la resolución sobre la custodia.",
         sentencias=[("Sentencia definitiva de divorcio y custodia", "favorable", "2023-11-22")]),
    dict(numero_expediente="1290/2022", titulo="Divorcio contencioso con liquidación de sociedad conyugal — Herrera",
         tipo_asunto="familiar", sub_tipo="divorcio", juzgado=FAM, contrario_despacho="Bufete Salgado y Asociados",
         descripcion="Divorcio con custodia de un menor y liquidación de la sociedad conyugal con dos inmuebles.",
         fecha_inicio="2022-05-02", fecha_cierre="2023-09-14", resultado="parcial",
         resultado_notas="Se obtuvo la custodia, pero la sociedad conyugal se liquidó al 50% porque no acreditamos "
                         "aportaciones exclusivas. Lección: pedir desde la demanda estados de cuenta y avalúos.",
         sentencias=[("Sentencia de divorcio y guarda y custodia", "favorable", "2023-02-20"),
                     ("Interlocutoria de liquidación de sociedad conyugal", "desfavorable", "2023-09-14")]),
    dict(numero_expediente="377/2024", titulo="Aumento de pensión alimenticia por cambio de circunstancias",
         tipo_asunto="familiar", sub_tipo="alimentos", juzgado="Juzgado Quinto de lo Familiar de la CDMX",
         descripcion="Incidente para aumentar la pensión de dos menores; el deudor oculta ingresos por honorarios.",
         fecha_inicio="2024-02-12", fecha_cierre="2024-08-30", resultado="favorable",
         resultado_notas="Probar el ingreso real con el informe del IMSS y los estados de cuenta, no con el recibo de "
                         "nómina, fue decisivo: se fijó el 30% de todas sus percepciones.",
         sentencias=[("Interlocutoria que aumenta la pensión al 30%", "favorable", "2024-08-30")]),
    dict(numero_expediente="512/2024", titulo="Cancelación de pensión alimenticia solicitada por el deudor",
         tipo_asunto="familiar", sub_tipo="alimentos", juzgado="Juzgado Quinto de lo Familiar de la CDMX",
         descripcion="El deudor pidió cancelar la pensión de un hijo mayor de edad que sigue estudiando.",
         fecha_inicio="2024-04-08", fecha_cierre="2024-10-15", resultado="desfavorable",
         resultado_notas="No ofrecimos a tiempo las constancias de estudios; el juez tuvo por acreditado que el "
                         "acreedor ya no estudiaba. Pedir constancias vigentes al cliente desde el primer día.",
         sentencias=[("Sentencia que cancela la pensión", "desfavorable", "2024-10-15")]),
    dict(numero_expediente="2201/2023", titulo="Ordinario mercantil por pago de obra — Grupo Inmobiliario Altavista",
         tipo_asunto="mercantil", sub_tipo="cobro", juzgado=MER, contrario_despacho="Litigio Corporativo Ibarra, S.C.",
         descripcion="Cobro de obra ejecutada y estimaciones no pagadas por un desarrollador inmobiliario.",
         fecha_inicio="2023-01-16", fecha_cierre="2024-03-28", resultado="favorable", monto_estimado=2850000,
         resultado_notas="Las bitácoras de obra firmadas por el residente del cliente y las estimaciones aceptadas por "
                         "correo probaron la recepción de los trabajos; el perito en ingeniería confirmó un avance "
                         "del 92%.",
         sentencias=[("Sentencia definitiva: condena al pago de estimaciones", "favorable", "2024-01-19"),
                     ("Resolución de apelación que confirma", "favorable", "2024-03-28")]),
    dict(numero_expediente="88/2024", titulo="Ejecutivo mercantil por cobro de pagarés — Distribuidora del Bajío",
         tipo_asunto="mercantil", sub_tipo="cobro", juzgado="Juzgado Cuarto de lo Civil de la CDMX",
         descripcion="Cobro de seis pagarés vencidos de un cliente de suministro de materiales.",
         fecha_inicio="2024-01-22", fecha_cierre="2024-06-05", resultado="convenio", monto_estimado=640000,
         resultado_notas="El embargo de cuentas llevó al deudor a un convenio judicial de pago en seis meses con "
                         "garantía hipotecaria.",
         sentencias=[("Auto que aprueba el convenio judicial", "favorable", "2024-06-05")]),
    dict(numero_expediente="1534/2022", titulo="Rescisión de contrato de suministro (parte demandada)",
         tipo_asunto="mercantil", sub_tipo="contratos", juzgado=MER, contrario_despacho="Litigio Corporativo Ibarra, S.C.",
         descripcion="El cliente fue demandado por incumplir plazos de entrega de un contrato de suministro de obra.",
         fecha_inicio="2022-08-01", fecha_cierre="2023-07-10", resultado="desfavorable",
         resultado_notas="Se aplicaron las penas convencionales porque los retrasos no se notificaron por escrito, "
                         "como exigía el contrato. Revisar siempre las cláusulas de notificación.",
         sentencias=[("Sentencia definitiva: rescisión y penas convencionales", "desfavorable", "2023-07-10")]),
    dict(numero_expediente="733/2023", titulo="Amparo indirecto contra clausura de establecimiento",
         tipo_asunto="amparo", sub_tipo="administrativo", juzgado=AMP,
         descripcion="Clausura de un restaurante por la alcaldía con un acta de visita sin fundamentación suficiente.",
         fecha_inicio="2023-06-05", fecha_cierre="2023-12-01", resultado="favorable",
         resultado_notas="Se concedió la suspensión definitiva y después el amparo por falta de fundamentación y "
                         "motivación del acta de visita: el inspector no se identificó con credencial vigente.",
         sentencias=[("Sentencia que concede el amparo", "favorable", "2023-12-01")]),
    dict(numero_expediente="410/2024", titulo="Amparo contra negativa de licencia de construcción",
         tipo_asunto="amparo", sub_tipo="administrativo", juzgado=AMP,
         descripcion="Negativa de licencia de construcción sin motivar, para un edificio de seis departamentos.",
         fecha_inicio="2024-03-11", fecha_cierre="2024-11-20", resultado="parcial",
         resultado_notas="Amparo para efectos: la autoridad tuvo que emitir una resolución nueva y fundada; al final "
                         "la licencia salió con condiciones de estacionamiento.",
         sentencias=[("Sentencia que concede el amparo para efectos", "parcial", "2024-11-20")]),
    dict(numero_expediente="CDMX/T/1180/2024", titulo="Despido injustificado de gerente de ventas",
         tipo_asunto="laboral", sub_tipo="despido", juzgado="Tribunal Laboral de la Ciudad de México",
         descripcion="Despido sin aviso por escrito de un gerente con ocho años de antigüedad.",
         fecha_inicio="2024-05-06", fecha_cierre="2024-07-18", resultado="convenio",
         resultado_notas="En la conciliación prejudicial se obtuvo el 90% de la indemnización constitucional más "
                         "salarios devengados, sin llegar a juicio.",
         sentencias=[]),
    dict(numero_expediente="CP-219/2024", titulo="Defensa penal por fraude genérico",
         tipo_asunto="penal", sub_tipo="fraude", juzgado="Juzgado de Control del Sistema Penal Acusatorio CDMX",
         descripcion="Imputación por fraude derivada de una compraventa de vehículo.", privado=1,
         fecha_inicio="2024-02-01", fecha_cierre="2024-09-12", resultado="favorable",
         resultado_notas="Suspensión condicional del proceso con plan de reparación del daño en 12 meses.",
         sentencias=[("Auto de suspensión condicional del proceso", "favorable", "2024-09-12")]),
]

ACTIVOS = {
    "Divorcio Contencioso Fuentes-Gómez": dict(
        juzgado=FAM, contrario_despacho="Bufete Salgado y Asociados", contrario_nombre="Laura Gómez Pérez",
        contrario_abogado="Lic. Andrés Salgado", sub_tipo="divorcio",
        descripcion="Divorcio sin acuerdo sobre la custodia de dos hijos menores y la liquidación de la sociedad "
                    "conyugal; la contraparte pide pensión compensatoria."),
    "Demanda Mercantil Constructora Omega": dict(
        juzgado=MER, contrario_despacho="Litigio Corporativo Ibarra, S.C.",
        contrario_nombre="Desarrollos Altamira, S.A. de C.V.", contrario_abogado="Lic. Patricia Ibarra",
        sub_tipo="cobro", monto_estimado=3200000, honorarios_pactados=280000,
        descripcion="Cobro de obra ejecutada y estimaciones no pagadas por el desarrollador; hay bitácora de obra "
                    "y correos de aceptación de las estimaciones."),
    "Amparo Administrativo — Moreno Ruiz": dict(
        juzgado=AMP, sub_tipo="administrativo",
        descripcion="Amparo indirecto contra la orden de clausura de su consultorio emitida por la alcaldía, con un "
                    "acta de visita sin fundamentación."),
}


# ── Machotes Word ─────────────────────────────────────────────────────────────

def _doc():
    d = Document()
    estilo = d.styles["Normal"]
    estilo.font.name = "Arial"
    estilo.font.size = Pt(11)
    return d


def _p(d, texto="", negrita=False, centro=False, derecha=False):
    p = d.add_paragraph()
    if centro:
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    elif derecha:
        p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    else:
        p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    if texto:
        p.add_run(texto).bold = negrita
    return p


def contrato() -> bytes:
    d = _doc()
    _p(d, "CONTRATO DE PRESTACIÓN DE SERVICIOS PROFESIONALES", negrita=True, centro=True)
    p = _p(d, "Que celebran por una parte ")
    # Campo partido en dos fragmentos con formato distinto, como lo deja Word a veces.
    p.add_run("{{nombre_").bold = True
    p.add_run("cliente}}").bold = True
    p.add_run(", en lo sucesivo «EL CLIENTE», con domicilio en {{domicilio_cliente}} y RFC {{rfc_cliente}}; "
              "y por la otra {{despacho}}, representado por {{nombre_abogado}}, en lo sucesivo «EL DESPACHO», "
              "al tenor de las siguientes:")
    _p(d, "CLÁUSULAS", negrita=True, centro=True)
    _p(d, "PRIMERA. Objeto. EL DESPACHO prestará a EL CLIENTE sus servicios profesionales para atender el asunto "
          "«{{titulo_asunto}}», expediente {{numero_expediente}}, radicado ante el {{juzgado}}.")
    _p(d, "SEGUNDA. Honorarios. EL CLIENTE pagará por concepto de honorarios la cantidad de {{honorarios}} "
          "({{honorarios_letra}}), más el Impuesto al Valor Agregado, pagaderos {{forma_de_pago}}.")
    _p(d, "TERCERA. Gastos. Los gastos y derechos que se generen correrán por cuenta de EL CLIENTE, previa "
          "comprobación.")
    _p(d, "CUARTA. Confidencialidad. EL DESPACHO guardará absoluta reserva de la información que reciba con motivo "
          "de este contrato, en términos del secreto profesional.")
    _p(d, "Leído que fue el presente contrato, lo firman las partes en la Ciudad de México, el {{fecha_hoy_letra}}.")
    t = d.add_table(rows=2, cols=2)
    t.cell(0, 0).text, t.cell(0, 1).text = "EL CLIENTE", "EL DESPACHO"
    t.cell(1, 0).text, t.cell(1, 1).text = "\n\n{{nombre_cliente}}", "\n\n{{nombre_abogado}}"
    salida = io.BytesIO()
    d.save(salida)
    return salida.getvalue()


def pruebas() -> bytes:
    d = _doc()
    d.sections[0].header.paragraphs[0].text = "Expediente: {{numero_expediente}}"
    d.sections[0].header.paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.RIGHT
    _p(d, "{{nombre_cliente}}", negrita=True)
    _p(d, "VS. {{contraparte}}", negrita=True)
    _p(d, "ESCRITO DE OFRECIMIENTO DE PRUEBAS", negrita=True, derecha=True)
    _p(d, "C. JUEZ DEL {{juzgado}}", negrita=True)
    _p(d, "P R E S E N T E.", negrita=True)
    _p(d, "{{nombre_abogado}}, en mi carácter de abogado patrono de {{nombre_cliente}}, personalidad que tengo "
          "debidamente acreditada en autos del expediente al rubro citado, ante Usted con el debido respeto "
          "comparezco para exponer:")
    _p(d, "Que estando en tiempo y forma, vengo a ofrecer de parte de mi representada las siguientes:")
    _p(d, "P R U E B A S", negrita=True, centro=True)
    _p(d, "1. LA DOCUMENTAL, consistente en {{documental}}.")
    _p(d, "2. LA TESTIMONIAL, a cargo de {{testigos}}, a quienes me comprometo a presentar el día que se señale "
          "para su desahogo.")
    _p(d, "3. LA INSTRUMENTAL DE ACTUACIONES y LA PRESUNCIONAL, en su doble aspecto legal y humano, en todo lo que "
          "favorezca a mi representada.")
    _p(d, "Por lo expuesto, a Usted C. Juez, atentamente pido se sirva tener por ofrecidas las pruebas señaladas y "
          "admitirlas por estar ofrecidas conforme a derecho.")
    _p(d, "PROTESTO LO NECESARIO", negrita=True, centro=True)
    _p(d, "Ciudad de México, a {{fecha_hoy_letra}}", centro=True)
    _p(d, "\n\n{{nombre_abogado}}", centro=True)
    salida = io.BytesIO()
    d.save(salida)
    return salida.getvalue()


def alimentos() -> bytes:
    d = _doc()
    _p(d, "{{nombre_cliente}}", negrita=True)
    _p(d, "VS. {{contraparte}}", negrita=True)
    _p(d, "JUICIO: CONTROVERSIA DEL ORDEN FAMILIAR (ALIMENTOS)", negrita=True, derecha=True)
    _p(d, "C. JUEZ DE LO FAMILIAR EN TURNO EN LA CIUDAD DE MÉXICO", negrita=True)
    _p(d, "P R E S E N T E.", negrita=True)
    _p(d, "{{nombre_cliente}}, por mi propio derecho y en representación de mi menor hijo(a) {{nombre_menor}}, "
          "señalando como domicilio para oír y recibir notificaciones el ubicado en {{domicilio_despacho}}, y "
          "autorizando en términos amplios a {{nombre_abogado}}, ante Usted con el debido respeto comparezco para "
          "exponer:")
    _p(d, "Que por medio del presente escrito vengo a demandar de {{contraparte}}, quien tiene su domicilio en "
          "{{domicilio_demandado}}, las siguientes:")
    _p(d, "P R E S T A C I O N E S", negrita=True, centro=True)
    _p(d, "A) El pago de una pensión alimenticia definitiva a favor de {{nombre_menor}}, equivalente al "
          "{{porcentaje_pension}} de las percepciones ordinarias y extraordinarias del demandado.")
    _p(d, "B) El aseguramiento de los alimentos en cualquiera de las formas previstas por la ley.")
    _p(d, "C) El pago de gastos y costas que el presente juicio origine.")
    _p(d, "Ciudad de México, a {{fecha_hoy_letra}}", centro=True)
    _p(d, "PROTESTO LO NECESARIO", negrita=True, centro=True)
    _p(d, "\n\n{{nombre_cliente}}", centro=True)
    salida = io.BytesIO()
    d.save(salida)
    return salida.getvalue()


MACHOTES = [
    ("Contrato de prestación de servicios profesionales", "Contratos", "Civil",
     "Contrato de honorarios con objeto, honorarios con letra, gastos y confidencialidad.", contrato),
    ("Escrito de ofrecimiento de pruebas", "Escritos", "Civil",
     "Ofrecimiento de documental, testimonial, instrumental y presuncional.", pruebas),
    ("Demanda de pensión alimenticia", "Demandas", "Familiar",
     "Controversia del orden familiar por alimentos con prestaciones y aseguramiento.", alimentos),
]


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tenant", default="bufete-ramirez")
    args = p.parse_args()

    app = create_app(os.environ.get("FLASK_ENV", "development"))
    with app.test_request_context("/", query_string={"tenant": args.tenant}):
        app.preprocess_request()
        if not getattr(g, "tenant", None):
            sys.exit(f"No existe el despacho {args.tenant!r}")
        tdb = g.tenant_db
        casos.asegurar(tdb)
        abogado = tdb.execute("SELECT abogado_responsable FROM asuntos WHERE abogado_responsable IS NOT NULL "
                              "LIMIT 1").fetchone()
        abogado_id = abogado[0] if abogado else None

        nuevos = 0
        for c in CONCLUIDOS:
            if tdb.execute("SELECT 1 FROM asuntos WHERE titulo=?", (c["titulo"],)).fetchone():
                continue
            datos = {k: v for k, v in c.items() if k != "sentencias"}
            datos.update(estado="cerrado", abogado_responsable=abogado_id, creado_por=abogado_id)
            cols = ", ".join(datos)
            cur = tdb.execute(f"INSERT INTO asuntos ({cols}) VALUES ({', '.join('?' * len(datos))})",
                              tuple(datos.values()))
            for titulo, sentido, fecha in c["sentencias"]:
                tdb.execute("INSERT INTO asunto_sentencias (asunto_id, titulo, tipo, sentido, fecha_emision, "
                            "creado_por) VALUES (?,?,?,?,?,?)",
                            (cur.lastrowid, titulo, "sentencia", sentido, fecha, abogado_id))
            nuevos += 1

        for titulo, campos in ACTIVOS.items():
            fila = tdb.execute("SELECT * FROM asuntos WHERE titulo=?", (titulo,)).fetchone()
            if not fila:
                continue
            vacios = {k: v for k, v in campos.items() if not fila[k]}
            if vacios:
                tdb.execute(f"UPDATE asuntos SET {', '.join(k + '=?' for k in vacios)} WHERE id=?",
                            (*vacios.values(), fila["id"]))
        tdb.commit()

        subidos = 0
        for nombre, categoria, derecho, descripcion, generar in MACHOTES:
            if tdb.execute("SELECT 1 FROM biblioteca_plantillas WHERE nombre=?", (nombre,)).fetchone():
                continue
            archivo = FileStorage(io.BytesIO(generar()), filename=f"{nombre}.docx",
                                  content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
            info = boveda.guardar(archivo, "biblioteca", "plantillas")
            tdb.execute("INSERT INTO biblioteca_plantillas (nombre, categoria, descripcion, tipo_derecho, archivo_url, "
                        "archivo_nombre, tamanio, extension, es_custom, creado_por) VALUES (?,?,?,?,?,?,?,?,1,?)",
                        (nombre, categoria, descripcion, derecho, info["url"], info["nombre"], info["tamanio"],
                         "docx", abogado_id))
            subidos += 1
        tdb.commit()
        print(f"Asuntos concluidos nuevos: {nuevos} · machotes subidos: {subidos}")


if __name__ == "__main__":
    main()
