from flask import Flask

from .config import load_settings
from .db import get_database_path
from .routes import bp as ui_blueprint


def create_app() -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    settings = load_settings()

    app.config.update(
        SECRET_KEY=settings.secret_key,
        ROOT_DIR=str(settings.root_dir),
        CONFIG_DIR=str(settings.config_dir),
        SOURCES_DIR=str(settings.sources_dir),
        SOURCES_CONFIG_PATH=str(settings.sources_config_path),
        DATA_DIR=str(settings.data_dir),
        RAW_DIR=str(settings.raw_dir),
        CLEANED_DIR=str(settings.cleaned_dir),
        DERIVED_DIR=str(settings.derived_dir),
        LOG_DIR=str(settings.log_dir),
        INSTANCE_DIR=str(settings.instance_dir),
        DATABASE_PATH=str(get_database_path()),
    )
    app.register_blueprint(ui_blueprint)

    return app
