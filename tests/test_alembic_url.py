"""Alembic must accept URL-encoded credentials without connecting to a database."""

import runpy
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import sqlalchemy.ext.asyncio as sa_async
from alembic.config import Config

from alembic import context


class AlembicUrlTests(unittest.TestCase):
    def test_database_url_survives_config_interpolation(self):
        for password in ("local-test", "local%40test%25%2F%23"):
            with self.subTest(password=password):
                url = f"postgresql+asyncpg://test_user:{password}@localhost/test_db"
                settings_module = ModuleType("app.core.config")
                settings_module.settings = SimpleNamespace(database_url=url)
                models_module = ModuleType("app.models")
                models_module.Base = SimpleNamespace(metadata=object())
                config = Config()
                with (
                    patch.dict(
                        sys.modules,
                        {"app.core.config": settings_module, "app.models": models_module},
                    ),
                    patch.object(context, "config", config, create=True),
                    patch.object(context, "is_offline_mode", return_value=True),
                    patch.object(context, "configure") as configure,
                    patch.object(context, "begin_transaction"),
                    patch.object(context, "run_migrations"),
                    patch.object(sa_async, "async_engine_from_config") as engine,
                ):
                    runpy.run_path(str(Path(__file__).parents[1] / "alembic/env.py"))
                self.assertEqual(config.get_main_option("sqlalchemy.url"), url)
                self.assertEqual(configure.call_args.kwargs["url"], url)
                engine.assert_not_called()


if __name__ == "__main__":
    unittest.main()
