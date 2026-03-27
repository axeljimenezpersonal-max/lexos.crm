"""
LexOS — modules/correo.py
Modulo de correo electrónico con Gmail OAuth2.
- Conexión OAuth: connect, callback, disconnect
- Envío via Gmail API (desde la cuenta real del abogado)
- Autocomplete de contactos
- Fallback a Flask-Mail si no hay Gmail conectado
"""

from __future__ import annotations

import base64
import json
import os
import uuid
from datetime import datetime, timedelta
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email import encoders

from flask import (
    Blueprint, current_app, flash, g, jsonify, redirect,
    render_template, request, session, url_for,
)

from modules.auth import login_required, tenant_required

correo_bp = Blueprint("correo", __name__)

# ── Gmail OAuth2 scopes ──────────────────────────────────────────────────────
GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]


# ═══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _get_integration(gdb, uid, tid):
    """Get Gmail integration for current user."""
    return gdb.execute(
        "SELECT * FROM user_integrations WHERE user_id=? AND tenant_id=? AND servicio='gmail' AND activo=1",
        (uid, tid),
    ).fetchone()


def _refresh_if_needed(gdb, integration):
    """Refresh access token if expired. Returns updated access_token or None."""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    cid = current_app.config.get("GOOGLE_CLIENT_ID", "")
    csecret = current_app.config.get("GOOGLE_CLIENT_SECRET", "")
    if not cid or not csecret:
        return None

    token_expiry = integration["token_expiry"]
    if token_expiry:
        try:
            exp = datetime.fromisoformat(token_expiry)
            if exp > datetime.utcnow() + timedelta(minutes=2):
                return integration["access_token"]
        except Exception:
            pass

    # Need refresh
    refresh_token = integration["refresh_token"]
    if not refresh_token:
        return None

    try:
        creds = Credentials(
            token=integration["access_token"],
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=cid,
            client_secret=csecret,
            scopes=GMAIL_SCOPES,
        )
        creds.refresh(Request())

        gdb.execute(
            "UPDATE user_integrations SET access_token=?, token_expiry=? WHERE id=?",
            (creds.token, creds.expiry.isoformat() if creds.expiry else None, integration["id"]),
        )
        gdb.commit()
        return creds.token
    except Exception as exc:
        current_app.logger.warning(f"[Gmail] Token refresh failed: {exc}")
        # Mark as inactive
        gdb.execute("UPDATE user_integrations SET activo=0 WHERE id=?", (integration["id"],))
        gdb.commit()
        return None


def _get_gmail_service(gdb, uid, tid):
    """Build Gmail API service for user, or return None."""
    integration = _get_integration(gdb, uid, tid)
    if not integration:
        return None, None

    access_token = _refresh_if_needed(gdb, integration)
    if not access_token:
        return None, None

    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials(token=access_token)
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    return service, integration


def _build_mime(from_email, from_name, to_list, cc_list, bcc_list,
                subject, html_body, text_body, attachments=None, firma_html=""):
    """Build MIME message for Gmail API."""
    msg = MIMEMultipart("mixed")
    msg["From"] = f"{from_name} <{from_email}>"
    msg["To"] = ", ".join(to_list)
    if cc_list:
        msg["Cc"] = ", ".join(cc_list)
    msg["Subject"] = subject

    # Body with firma
    full_html = html_body
    if firma_html:
        full_html += f'<br><div style="margin-top:16px;padding-top:12px;border-top:1px solid #e5e7eb;color:#6b7280;font-size:13px">{firma_html}</div>'

    # Text alternative
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(text_body or html_body, "plain", "utf-8"))
    alt.attach(MIMEText(full_html, "html", "utf-8"))
    msg.attach(alt)

    # Attachments
    if attachments:
        for att in attachments:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(att["data"])
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", f'attachment; filename="{att["filename"]}"')
            msg.attach(part)

    return base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")


# ═══════════════════════════════════════════════════════════════════════════════
#  OAUTH2 FLOW
# ═══════════════════════════════════════════════════════════════════════════════

@correo_bp.route("/gmail/connect")
@login_required
@tenant_required
def gmail_connect():
    """Start Gmail OAuth2 flow (manual, no PKCE issues)."""
    import urllib.parse

    cid = current_app.config.get("GOOGLE_CLIENT_ID", "")
    csecret = current_app.config.get("GOOGLE_CLIENT_SECRET", "")
    if not cid or not csecret:
        flash("Google OAuth no configurado. Agrega GOOGLE_CLIENT_ID y GOOGLE_CLIENT_SECRET en .env", "danger")
        return redirect(request.referrer or url_for("dashboard.index"))

    redirect_uri = request.host_url.rstrip("/") + "/correo/gmail/callback"
    redirect_uri = redirect_uri.replace("127.0.0.1", "localhost")

    state = uuid.uuid4().hex
    session["gmail_oauth_state"] = state
    session["gmail_oauth_redirect"] = request.referrer or url_for("dashboard.index")
    session["gmail_redirect_uri"] = redirect_uri

    params = {
        "client_id": cid,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(GMAIL_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)
    return redirect(auth_url)


@correo_bp.route("/gmail/callback")
@login_required
@tenant_required
def gmail_callback():
    """Handle Gmail OAuth2 callback (manual token exchange, no PKCE)."""
    import requests as req

    code = request.args.get("code")
    if not code:
        flash("Error de autenticación con Gmail.", "danger")
        return redirect(url_for("dashboard.index"))

    cid = current_app.config.get("GOOGLE_CLIENT_ID", "")
    csecret = current_app.config.get("GOOGLE_CLIENT_SECRET", "")
    redirect_uri = session.get("gmail_redirect_uri",
                               request.host_url.rstrip("/") + "/correo/gmail/callback")
    redirect_uri = redirect_uri.replace("127.0.0.1", "localhost")

    # Exchange code for tokens
    token_resp = req.post("https://oauth2.googleapis.com/token", data={
        "code": code,
        "client_id": cid,
        "client_secret": csecret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    })
    token_data = token_resp.json()

    if "error" in token_data:
        current_app.logger.warning(f"[Gmail OAuth] Token error: {token_data}")
        flash(f"Error Gmail: {token_data.get('error_description', token_data['error'])}", "danger")
        return redirect(url_for("dashboard.index"))

    access_token = token_data["access_token"]
    refresh_token = token_data.get("refresh_token")
    expires_in = token_data.get("expires_in", 3600)
    token_expiry = (datetime.utcnow() + timedelta(seconds=expires_in)).isoformat()

    # Get user info
    userinfo_resp = req.get("https://www.googleapis.com/oauth2/v2/userinfo",
                            headers={"Authorization": f"Bearer {access_token}"})
    userinfo = userinfo_resp.json()
    gmail_email = userinfo.get("email", "")
    gmail_name = userinfo.get("name", "")

    # Save to DB
    gdb = g.global_db
    uid = session.get("user_id")
    tid = g.tenant["id"]

    existing = gdb.execute(
        "SELECT id FROM user_integrations WHERE user_id=? AND tenant_id=? AND servicio='gmail'",
        (uid, tid),
    ).fetchone()

    if existing:
        gdb.execute(
            """UPDATE user_integrations
               SET access_token=?, refresh_token=?, token_expiry=?,
                   email_cuenta=?, nombre_cuenta=?, activo=1
               WHERE id=?""",
            (access_token, refresh_token, token_expiry, gmail_email, gmail_name, existing["id"]),
        )
    else:
        gdb.execute(
            """INSERT INTO user_integrations
               (tenant_id, user_id, servicio, access_token, refresh_token,
                token_expiry, email_cuenta, nombre_cuenta, activo)
               VALUES (?,?,?,?,?,?,?,?,1)""",
            (tid, uid, "gmail", access_token, refresh_token, token_expiry, gmail_email, gmail_name),
        )
    gdb.commit()

    flash(f"Gmail conectado: {gmail_email}", "success")
    return redirect(session.pop("gmail_oauth_redirect", url_for("dashboard.index")))


@correo_bp.route("/gmail/disconnect", methods=["POST"])
@login_required
@tenant_required
def gmail_disconnect():
    """Disconnect Gmail integration."""
    gdb = g.global_db
    uid = session.get("user_id")
    tid = g.tenant["id"]

    integration = _get_integration(gdb, uid, tid)
    if integration:
        # Revoke token
        try:
            import requests as req
            req.post(
                "https://oauth2.googleapis.com/revoke",
                params={"token": integration["access_token"]},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except Exception:
            pass
        gdb.execute("DELETE FROM user_integrations WHERE id=?", (integration["id"],))
        gdb.commit()

    return jsonify(ok=True, msg="Gmail desconectado")


@correo_bp.route("/gmail/status")
@login_required
@tenant_required
def gmail_status():
    """Check if user has Gmail connected."""
    gdb = g.global_db
    uid = session.get("user_id")
    tid = g.tenant["id"]

    integration = _get_integration(gdb, uid, tid)
    if integration:
        return jsonify(
            connected=True,
            email=integration["email_cuenta"],
            nombre=integration["nombre_cuenta"],
        )
    return jsonify(connected=False)


# ═══════════════════════════════════════════════════════════════════════════════
#  ENVIAR CORREO
# ═══════════════════════════════════════════════════════════════════════════════

@correo_bp.route("/enviar", methods=["POST"])
@login_required
@tenant_required
def enviar():
    """Send email via Gmail API (or Flask-Mail fallback)."""
    gdb = g.global_db
    tdb = g.tenant_db
    uid = session.get("user_id")
    tid = g.tenant["id"]

    data = request.json or {}
    to_list = data.get("para", [])
    cc_list = data.get("cc", [])
    bcc_list = data.get("cco", [])
    subject = (data.get("asunto") or "").strip()
    html_body = (data.get("cuerpo_html") or "").strip()
    asunto_id = data.get("asunto_id")

    if not to_list or not subject:
        return jsonify(ok=False, error="Destinatario y asunto son obligatorios"), 400

    # Get user info
    user = gdb.execute(
        "SELECT nombre, apellidos, email FROM tenant_users WHERE id=?", (uid,)
    ).fetchone()
    user_name = f"{user['nombre']} {user['apellidos'] or ''}" if user else "LexOS"

    # Get firma
    firma_html = ""
    try:
        firma_row = gdb.execute(
            "SELECT firma_email FROM tenant_users WHERE id=?", (uid,)
        ).fetchone()
        firma_html = firma_row["firma_email"] or "" if firma_row else ""
    except Exception:
        pass

    # Try Gmail API first
    service, integration = _get_gmail_service(gdb, uid, tid)
    gmail_message_id = None
    gmail_thread_id = None
    sent_via = "gmail"

    if service:
        try:
            from_email = integration["email_cuenta"]
            from_name = integration["nombre_cuenta"] or user_name

            raw = _build_mime(
                from_email, from_name,
                to_list, cc_list, bcc_list,
                subject, html_body,
                html_body,  # text fallback
                firma_html=firma_html,
            )

            result = service.users().messages().send(
                userId="me",
                body={"raw": raw},
            ).execute()

            gmail_message_id = result.get("id")
            gmail_thread_id = result.get("threadId")

        except Exception as exc:
            current_app.logger.warning(f"[Gmail API] Send failed: {exc}")
            import traceback
            traceback.print_exc()
            # Fall back to Flask-Mail
            service = None
            sent_via = "flask-mail"

    # Fallback: Flask-Mail
    if not service:
        try:
            from app import mail
            from flask_mail import Message

            full_html = html_body
            if firma_html:
                full_html += f'<br><div style="margin-top:16px;padding-top:12px;border-top:1px solid #e5e7eb;color:#6b7280;font-size:13px">{firma_html}</div>'

            msg = Message(
                subject=subject,
                recipients=to_list,
                cc=cc_list or None,
                bcc=bcc_list or None,
                html=full_html,
                reply_to=user["email"] if user else None,
            )
            mail.send(msg)
            sent_via = "flask-mail"
        except Exception as exc:
            current_app.logger.warning(f"[Flask-Mail] Send failed: {exc}")
            return jsonify(ok=False, error="No se pudo enviar. Conecta Gmail o configura SMTP."), 500

    # Log in asunto timeline
    if asunto_id:
        try:
            tdb.execute(
                """INSERT INTO asunto_notas (asunto_id, contenido, menciones, creado_por)
                   VALUES (?, ?, '[]', ?)""",
                (
                    asunto_id,
                    json.dumps({
                        "tipo": "correo_enviado",
                        "para": to_list,
                        "cc": cc_list,
                        "asunto": subject,
                        "gmail_message_id": gmail_message_id,
                        "gmail_thread_id": gmail_thread_id,
                        "sent_via": sent_via,
                    }, ensure_ascii=False),
                    uid,
                ),
            )
            tdb.commit()
        except Exception:
            pass

    # Log in actividad
    try:
        tdb.execute(
            "INSERT INTO actividad_log (user_id, accion, entidad, entidad_id, detalles, ip) "
            "VALUES (?,?,?,?,?,?)",
            (
                uid, "enviar_correo", "asunto" if asunto_id else "correo",
                asunto_id,
                json.dumps({
                    "para": to_list, "cc": cc_list, "asunto": subject,
                    "gmail_message_id": gmail_message_id,
                    "sent_via": sent_via,
                }, ensure_ascii=False),
                request.remote_addr,
            ),
        )
        tdb.commit()
    except Exception:
        pass

    return jsonify(
        ok=True,
        message_id=gmail_message_id,
        thread_id=gmail_thread_id,
        sent_via=sent_via,
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  AUTOCOMPLETE CONTACTOS
# ═══════════════════════════════════════════════════════════════════════════════

@correo_bp.route("/contactos/buscar")
@login_required
@tenant_required
def buscar_contactos():
    """Search contacts for email autocomplete."""
    q = (request.args.get("q") or "").strip().lower()
    if len(q) < 1:
        return jsonify([])

    tdb = g.tenant_db
    gdb = g.global_db
    tid = g.tenant["id"]
    like = f"%{q}%"
    results = []

    # 1. Clientes
    try:
        for r in tdb.execute(
            """SELECT id, nombre, apellidos, email, razon_social
               FROM clientes
               WHERE activo=1 AND email IS NOT NULL AND email != ''
                 AND (LOWER(nombre) LIKE ? OR LOWER(apellidos) LIKE ?
                      OR LOWER(email) LIKE ? OR LOWER(razon_social) LIKE ?)
               LIMIT 4""",
            (like, like, like, like),
        ).fetchall():
            r = dict(r)
            nombre = (r.get("razon_social") or f"{r['nombre']} {r.get('apellidos', '')}").strip()
            results.append({
                "nombre": nombre,
                "email": r["email"],
                "tipo": "cliente",
                "avatar_iniciales": nombre[:2].upper(),
                "color": "#3b82f6",
            })
    except Exception:
        pass

    # 2. Equipo (tenant_users)
    try:
        for r in gdb.execute(
            """SELECT id, nombre, apellidos, email
               FROM tenant_users
               WHERE tenant_id=? AND activo=1
                 AND (LOWER(nombre) LIKE ? OR LOWER(apellidos) LIKE ? OR LOWER(email) LIKE ?)
               LIMIT 4""",
            (tid, like, like, like),
        ).fetchall():
            r = dict(r)
            nombre = f"{r['nombre']} {r.get('apellidos', '')}".strip()
            results.append({
                "nombre": nombre,
                "email": r["email"],
                "tipo": "equipo",
                "avatar_iniciales": f"{r['nombre'][0]}{(r.get('apellidos') or 'X')[0]}".upper(),
                "color": "#8b5cf6",
            })
    except Exception:
        pass

    # 3. Contrapartes from asuntos
    try:
        for r in tdb.execute(
            """SELECT DISTINCT contrario_nombre, contrario_abogado
               FROM asuntos
               WHERE (LOWER(contrario_nombre) LIKE ? OR LOWER(contrario_abogado) LIKE ?)
                 AND contrario_nombre IS NOT NULL
               LIMIT 3""",
            (like, like),
        ).fetchall():
            r = dict(r)
            nombre = r.get("contrario_nombre") or r.get("contrario_abogado") or ""
            if nombre:
                results.append({
                    "nombre": nombre,
                    "email": "",  # Contrapartes may not have email
                    "tipo": "contraparte",
                    "avatar_iniciales": nombre[:2].upper(),
                    "color": "#f59e0b",
                })
    except Exception:
        pass

    # Deduplicate by email
    seen = set()
    deduped = []
    for r in results:
        key = r["email"].lower() if r["email"] else r["nombre"]
        if key not in seen:
            seen.add(key)
            deduped.append(r)

    return jsonify(deduped[:8])
