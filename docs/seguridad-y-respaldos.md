# Seguridad y respaldos de Habeas Corpus

Explicado sin tecnicismos: cómo se protege la información de los despachos, cómo se respalda, qué pasa si se pierde la llave y cómo se recupera todo.

---

## 1. Cifrado: quién puede leer qué

**Los documentos** (PDF, Word, fotos…) que suben los despachos se guardan **cifrados**. En el disco del servidor no son documentos, son datos revueltos. Solo el sistema, con la llave correcta, los vuelve legibles, y solo se los entrega a usuarios con sesión en ese despacho (o a sus clientes cuando el despacho se los comparte).

- Hay **una llave maestra**, `BOVEDA_MASTER_KEY`, que vive en las variables de Railway.
- De ella sale **una llave distinta para cada despacho**. Un documento de un despacho no se puede abrir con la llave de otro.

**Las bases de datos** (expedientes, clientes, agenda, cobranza) son un archivo por despacho, separado de los demás. Viven en el volumen de Railway. Ese archivo no está cifrado por la aplicación; lo protegen el acceso restringido al servidor y, en los respaldos, el cifrado del paquete (ver abajo).

**En tránsito**, todo viaja por HTTPS.

---

## 2. Respaldos

### Qué se respalda

Cada respaldo es **un solo archivo**, con fecha en el nombre, por ejemplo `respaldo-2026-10-29_090000.tar.gz.enc`. Dentro va:

| Qué | Cómo se copia |
|---|---|
| La base general (usuarios, despachos, aceptaciones de términos) | Copia consistente aunque el sistema esté en uso |
| La base de cada despacho | Igual |
| Todos los documentos de la bóveda | Tal cual: ya van cifrados |
| Una lista con la huella de cada archivo | Para comprobar al restaurar que nada se dañó |

No se respalda la base legal pública (leyes y tesis, ~350 MB) porque se puede volver a descargar. Si se quiere incluir: `RESPALDO_INCLUIR_CORPUS=1`.

El paquete completo **se cifra antes de salir del servidor** (AES-256), con una llave que sale de la misma `BOVEDA_MASTER_KEY`. Si alguien roba un respaldo, no puede leer nada sin esa llave.

### Cuántos se guardan

Se conservan **los últimos 7 días** y, antes de esos, **una copia por semana de 4 semanas más**: 11 copias, unas 5 semanas de historia. Los más viejos se borran solos. Así siempre hay a dónde regresar si alguien borra algo y tarda días en darse cuenta.

### Dónde se guardan

Lo decide la variable `RESPALDO_DESTINO`:

- **Un bucket S3-compatible** (recomendado): `s3://nombre-del-bucket/produccion`. Sirve cualquier servicio compatible con S3: Cloudflare R2, Backblaze B2, AWS S3, DigitalOcean Spaces. **Debe estar fuera de Railway**: si algo le pasa a Railway o a la cuenta, los respaldos siguen a salvo.
- **Una carpeta**: `/ruta/a/respaldos`. Útil en local. En Railway no protege de nada si está en el mismo volumen.

Si `RESPALDO_DESTINO` no está puesta, el script **avisa y no hace nada**; no truena.

### Variables

| Variable | Para qué |
|---|---|
| `RESPALDO_DESTINO` | Carpeta o `s3://bucket/prefijo` |
| `RESPALDO_S3_ENDPOINT` | Solo si no es AWS. Ej. R2: `https://<cuenta>.r2.cloudflarestorage.com` |
| `RESPALDO_S3_REGION` | Región del bucket (en R2: `auto`) |
| `RESPALDO_S3_ACCESS_KEY` / `RESPALDO_S3_SECRET_KEY` | Credenciales del bucket. Que solo puedan leer y escribir en ese bucket |
| `RESPALDO_TOKEN` | Contraseña larga y aleatoria para pedir el respaldo por internet (ver 2.1) |
| `BOVEDA_MASTER_KEY` | La llave. Ya existe; los respaldos usan la misma |

### Comandos

```bash
python scripts/respaldo.py                 # hace un respaldo ahora
python scripts/respaldo.py --listar        # muestra los respaldos guardados
python scripts/simulacro_respaldo.py       # simulacro completo, sin tocar los datos reales
```

### 2.1 Programarlo en Railway (propuesta, aún no configurada)

**El detalle:** en Railway el volumen con los datos solo está conectado al servicio web. Un servicio aparte no puede leer esos archivos. Por eso el respaldo corre **dentro del servicio web**, y un pequeño cron de Railway solo le "toca la puerta" una vez al día.

Pasos:

1. **Crear el bucket** fuera de Railway (por ejemplo, Cloudflare R2, que no cobra por descargar). Crear unas credenciales que solo tengan acceso a ese bucket.
2. **En el servicio web** de Railway, agregar las variables: `RESPALDO_DESTINO`, `RESPALDO_S3_ENDPOINT`, `RESPALDO_S3_REGION`, `RESPALDO_S3_ACCESS_KEY`, `RESPALDO_S3_SECRET_KEY` y `RESPALDO_TOKEN` (generar uno con `python -c "import secrets; print(secrets.token_urlsafe(32))"`).
3. **Crear un servicio nuevo** en el mismo proyecto, llamado `respaldo-diario`:
   - Origen: la imagen de Docker `curlimages/curl`.
   - Variables: `RESPALDO_TOKEN` (la misma del paso 2, se puede referenciar con `${{web.RESPALDO_TOKEN}}`) y `RESPALDO_URL=https://habeas-corpus-production.up.railway.app/interno/respaldo`.
   - Comando de inicio:
     ```bash
     sh -c 'curl -fsS -X POST -H "Authorization: Bearer $RESPALDO_TOKEN" "$RESPALDO_URL"'
     ```
   - En **Settings → Cron Schedule**: `0 9 * * *` (todos los días a las 9:00 UTC = 3:00 a. m. hora del centro de México).
4. Al día siguiente, revisar que el respaldo exista en el bucket, o consultar el estado:
   ```bash
   curl -H "Authorization: Bearer $RESPALDO_TOKEN" https://habeas-corpus-production.up.railway.app/interno/respaldo
   ```
   Responde `"estado": "ok"` con el nombre y tamaño del último respaldo, o `"error"` con el motivo.

Sin `RESPALDO_TOKEN` la dirección `/interno/respaldo` no existe (responde 404). Con un token equivocado, también 404.

**Complemento:** Railway ofrece además respaldos propios del volumen en algunos planes (Volume → Backups). Conviene activarlos, pero **no sustituyen** a estos: viven dentro de la misma cuenta de Railway.

---

## 3. Si se pierde la llave

> **`BOVEDA_MASTER_KEY` es la única llave. Si se pierde, nadie puede recuperar los documentos. Ni nosotros, ni Railway, ni nadie.**

Qué se pierde y qué no:

| Si se pierde la llave… | ¿Se recupera? |
|---|---|
| Documentos de la bóveda (en el servidor) | **No.** Quedan cifrados para siempre |
| Respaldos | **No.** Están cifrados con la misma llave |
| Bases de datos que siguen en el volumen | Sí, mientras el volumen exista |

Por eso:

- **No cambies nunca la llave** en Railway. Cambiarla equivale a perderla: los documentos ya guardados dejarían de abrirse.
- **Respáldala fuera de Railway**, hoy mismo (recomendación):
  1. En un gestor de contraseñas (1Password, Bitwarden) en una bóveda compartida con un segundo responsable de confianza.
  2. Una copia **en papel**, en un sobre cerrado, en una caja fuerte o con el notario/abogado de la empresa.
  3. Nunca en el repositorio de código, en un correo, en un chat ni en el mismo bucket que los respaldos.
- Una vez al año, comprueba que la llave guardada sea la misma que la de Railway (sin mostrarla en pantalla: compara las primeras 4 letras).

---

## 4. Cómo restaurar

El script **nunca borra nada**. Por defecto ni siquiera toca el sistema: descifra el respaldo en una carpeta aparte para revisarlo.

```bash
python scripts/restaurar.py --listar          # ver qué respaldos hay
python scripts/restaurar.py --ultimo          # extraer el más reciente a ./restauracion-<fecha>/
```

### Caso A: un despacho borró algo por error

Restaurar solo ese despacho (su base y sus documentos), sin tocar a los demás:

```bash
python scripts/restaurar.py --archivo respaldo-2026-10-27_090000.tar.gz.enc --despacho bufete-ramirez --en-sitio --confirmar
```

Ojo: esto regresa **todo** ese despacho a la fecha del respaldo. Si solo hace falta un expediente, es mejor extraer a una carpeta aparte (sin `--en-sitio`) y copiar a mano solo lo necesario.

### Caso B: se perdió el volumen completo

1. Crear un volumen nuevo en Railway y conectarlo al servicio web.
2. Verificar que `BOVEDA_MASTER_KEY` sea **la misma** de siempre.
3. Abrir una terminal en el servicio (`railway ssh`) y correr:
   ```bash
   python scripts/restaurar.py --ultimo --en-sitio --confirmar
   ```
4. Reiniciar el servicio en Railway.

Lo que hubiera en las carpetas antes de restaurar se mueve a `_antes_de_restaurar/<fecha>/`, por si acaso. Cuando todo esté bien, se puede borrar a mano.

Conviene restaurar fuera del horario de oficina: mientras se restaura, lo que alguien capture puede perderse.

### Simulacro

Una vez al mes, correr `python scripts/simulacro_respaldo.py`. Copia los datos a una carpeta temporal, respalda, borra la copia, restaura y comprueba que todo quedó idéntico y que los documentos se pueden abrir. No toca los datos reales. Un respaldo que nunca se ha probado no es un respaldo.
