from flask import Flask

def create_app():
    app = Flask(__name__)
    app.secret_key = 'super_secret_key_for_session_security'  # Bunu production'da .env'den almalısın

    # Route'ları import et ve kaydet
    from web.routes import main_bp
    app.register_blueprint(main_bp)

    return app