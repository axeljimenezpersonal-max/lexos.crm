# Respaldos en Amazon Web Services (AWS)

Cómo dejar los respaldos de Habeas Corpus en Amazon S3, paso a paso. Los archivos de permisos están en `infra/aws/`. Donde dicen `BUCKET`, se pone el nombre real del bucket.

## La idea en cuatro puntos

1. **Los respaldos viven en Amazon, fuera de Railway.** Si algo le pasa a Railway, las copias siguen a salvo.
2. **Se guardan en México**, en la región **México (Central)**, `mx-central-1`, en Querétaro. Es un buen argumento de venta para despachos y notarías.
3. **Nadie puede borrarlos antes de 35 días**, ni siquiera nosotros. Eso es el "bloqueo de objetos" en modo cumplimiento.
   - Si alguien entra al servidor o roba una contraseña, puede subir basura, pero no borrar los respaldos buenos.
   - Pasados 35 días, Amazon los borra solo.
4. **El servidor solo puede subir y leer, nunca borrar.** La limpieza de copias viejas la hace Amazon, no el programa (`RESPALDO_RETENCION=nube`).

Resultado:
- Hay **un respaldo diario de los últimos 35 días**.
- Cada copia va cifrada antes de salir del servidor con la llave `BOVEDA_MASTER_KEY`, así que en Amazon solo hay datos ilegibles.
- Con pocos GB, cuesta menos de un dólar al mes.

## Paso 1 — La cuenta de AWS, bien protegida

1. Crear la cuenta en aws.amazon.com con un correo de la empresa, no uno personal.
2. **Activar la verificación en dos pasos (MFA) en el usuario raíz.** Después de eso, el usuario raíz no se usa para el día a día.
3. En **IAM Identity Center**, crear un usuario administrador para Axel, también con MFA, y entrar siempre con ese.
4. En **Billing → Budgets**, poner una alerta de gasto, por ejemplo a los 10 dólares al mes, para enterarse si algo se dispara.

## Paso 2 — El bucket

En **S3 → Create bucket**:

| Opción | Valor |
|---|---|
| Región | **México (Central) — mx-central-1** |
| Nombre | `habeas-corpus-respaldos-<algo único>`; el nombre debe ser único en todo AWS |
| Block all public access | **Activado**, todo |
| Bucket Versioning | **Enable** |
| Default encryption | SSE-S3; ya va cifrado por nosotros, esto es una capa extra |
| Object Lock | **Enable**, y en *Default retention*: **Compliance**, **35 días** (igual que `infra/aws/bloqueo-de-objetos.json`) |

> El modo **Compliance** no se puede quitar ni acortar después, ni siquiera desde el usuario raíz. Por eso protege. Si se quiere probar primero, usar **Governance** unos días y luego cambiar a Compliance.

Después, dentro del bucket:

- **Management → Lifecycle rule**: crear la regla de `infra/aws/ciclo-de-vida.json`.
  - Las copias expiran a los 36 días, las versiones viejas se limpian al día siguiente y se descartan las subidas a medias.
- **Permissions → Bucket policy**: pegar `infra/aws/politica-bucket.json`, cambiando `BUCKET`. Obliga a que toda conexión vaya cifrada.

## Paso 3 — El usuario que usa el servidor

En **IAM → Users → Create user**:

1. Nombre: `habeas-respaldos-railway`. **Sin acceso a la consola.**
2. Darle permisos con una política nueva, pegando `infra/aws/politica-servidor-respaldos.json` y cambiando `BUCKET`.
   - Solo puede listar, subir y leer en la carpeta `produccion/`. No puede borrar nada.
3. En **Security credentials → Create access key** (tipo "Application running outside AWS"), copiar las dos claves.
   - Van directo a Railway; no se guardan en ningún otro lado.

## Paso 4 — Variables en Railway (servicio web)

| Variable | Valor |
|---|---|
| `RESPALDO_DESTINO` | `s3://BUCKET/produccion` |
| `RESPALDO_S3_REGION` | `mx-central-1` |
| `RESPALDO_S3_ACCESS_KEY` | la *Access key* del paso 3 |
| `RESPALDO_S3_SECRET_KEY` | la *Secret access key* del paso 3 |
| `RESPALDO_RETENCION` | `nube` |
| `RESPALDO_TOKEN` | una contraseña larga y aleatoria (ver `docs/seguridad-y-respaldos.md`, 2.1) |

`RESPALDO_S3_ENDPOINT` se deja vacía: en AWS no hace falta.

Luego, el cron diario que pide el respaldo, tal como dice `docs/seguridad-y-respaldos.md`, sección 2.1.

## Paso 5 — La llave maestra, fuera de Railway

Sin `BOVEDA_MASTER_KEY`, los respaldos y los documentos no se pueden abrir. Hay que guardarla en dos lugares además de Railway:

1. **AWS Secrets Manager**, misma región, con el nombre `habeas-corpus/boveda-master-key`. Cuesta unos centavos al mes.
2. **En papel o en una memoria USB, en una caja fuerte.** Si se pierde la cuenta de AWS, esta copia salva todo.

## Paso 6 — Comprobar que funciona

1. Pedir un respaldo manual (`docs/seguridad-y-respaldos.md`, 2.1) y ver que aparezca el archivo en `produccion/` del bucket.
2. Intentar borrarlo desde la consola: **debe negarse** por el bloqueo. Si se deja borrar, el Object Lock no quedó activo.
3. Una vez al mes, restaurar el respaldo más reciente en local con `scripts/restaurar.py` y abrir algún documento.
   - Para eso se crea aparte un usuario de solo lectura (`s3:GetObject` y `s3:ListBucket`), que nunca va a Railway.

## Más adelante en AWS

Este mismo esquema sirve si después se mueve a AWS el resto del sistema: servidor, base de datos, correo con SES o archivos. La cuenta ya queda protegida con MFA, alerta de gasto y un usuario por servicio con el mínimo de permisos.
