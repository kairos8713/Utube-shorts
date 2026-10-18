import smtplib
import json
import os
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'email_config.json')


def load_config():
    if not os.path.exists(CONFIG_PATH):
        print(f"[EMAIL] Konfigürasyon dosyası bulunamadı: {CONFIG_PATH}")
        return None
    try:
        with open(CONFIG_PATH, encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"[EMAIL] Config okunamadı: {e}")
        return None


def send_alert(subject: str, body: str):
    """
    Konfigürasyona göre e-posta bildirimi gönderir.
    email_config.json dosyasında 'enabled: true' olmalı.
    """
    config = load_config()

    if not config:
        print(f"[EMAIL] Config yok, bildirim atlandı: {subject}")
        return

    if not config.get('enabled', False):
        print(f"[EMAIL] Bildirimler devre dışı. ({subject})")
        return

    try:
        msg = MIMEMultipart('alternative')
        msg['From'] = config['from_email']
        msg['To'] = config['to_email']
        msg['Subject'] = f"[UTube Bot] {subject}"

        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        plain_body = f"{body}\n\n---\nBildirim Zamanı: {timestamp}\nUTube Otomasyonu"
        html_body = f"""
        <html><body style="font-family: Arial, sans-serif; color: #333;">
        <div style="background:#1a1a2e;color:#e0e0e0;padding:15px;border-radius:8px;margin-bottom:15px;">
            <h2 style="margin:0;color:#00d4ff;">🤖 UTube Bot Bildirimi</h2>
        </div>
        <div style="background:#f8f9fa;padding:20px;border-radius:8px;border-left:4px solid #00d4ff;">
            <h3 style="color:#1a1a2e;">{subject}</h3>
            <pre style="white-space:pre-wrap;font-family:Consolas,monospace;font-size:13px;color:#333;">{body}</pre>
        </div>
        <p style="color:#999;font-size:12px;margin-top:15px;">
            📅 {timestamp}<br>
            <em>Bu mesaj UTube Otomasyon Sistemi tarafından gönderilmiştir.</em>
        </p>
        </body></html>
        """

        msg.attach(MIMEText(plain_body, 'plain', 'utf-8'))
        msg.attach(MIMEText(html_body, 'html', 'utf-8'))

        smtp_host = config.get('smtp_host', 'smtp.gmail.com')
        smtp_port = config.get('smtp_port', 587)

        server = smtplib.SMTP(smtp_host, smtp_port, timeout=30)
        server.ehlo()
        server.starttls()
        server.login(config['from_email'], config['smtp_password'])
        server.send_message(msg)
        server.quit()

        print(f"[EMAIL] ✓ Bildirim gönderildi: {subject}")

    except smtplib.SMTPAuthenticationError:
        print("[EMAIL] ✗ Kimlik doğrulama hatası! Gmail kullanıyorsanız 'App Password' oluşturun.")
    except smtplib.SMTPException as e:
        print(f"[EMAIL] ✗ SMTP hatası: {e}")
    except Exception as e:
        print(f"[EMAIL] ✗ Bildirim gönderilemedi: {e}")
