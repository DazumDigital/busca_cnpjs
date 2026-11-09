
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class WebSettings:
    database_url: str = ''
    origin: str = 'http://127.0.0.1:8000'
    environment: str = 'local'
    external_enabled: bool = False
    session_hours: int = 8
    query_per_minute: int = 20
    serper_key: str = field(default='', repr=False)

    def __post_init__(self):
        parts = urlsplit(self.origin)

        if (
            parts.scheme not in {'http', 'https'}
            or not parts.hostname
            or parts.path
            or parts.query
            or parts.fragment
            or parts.username
        ):
            raise ValueError(
                'WEB_ORIGIN deve ser uma origem completa sem caminho'
            )

        local = (
            self.environment == 'local'
            and not os.getenv('VERCEL')
        )

        if (
            parts.scheme != 'https'
            and (
                not local
                or parts.hostname not in {
                    'localhost',
                    '127.0.0.1',
                    'testserver'
                }
            )
        ):
            raise ValueError(
                'HTTPS obrigatório fora do ambiente local'
            )

        if (
            self.database_url
            and not self.database_url.startswith((
                'sqlite:///',
                'postgresql+psycopg://'
            ))
        ):
            raise ValueError(
                'Use SQLite local ou PostgreSQL com psycopg'
            )

        if self.database_url.startswith('sqlite:'):
            if not local:
                raise ValueError(
                    'SQLite mutável não é permitido '
                    'em serverless/produção'
                )

            path = Path(
                self.database_url.removeprefix('sqlite:///')
            ).resolve()

            if path.is_relative_to(ROOT / 'data'):
                raise ValueError(
                    'Não utilizar os bancos históricos como banco web'
                )

        if self.database_url.startswith('postgresql+psycopg:'):
            db_url = urlsplit(self.database_url)

            remoto = (
                not local
                or db_url.hostname not in {
                    'localhost',
                    '127.0.0.1',
                    '::1'
                }
            )

            if (
                remoto
                and parse_qs(db_url.query).get('sslmode')
                != ['verify-full']
            ):
                raise ValueError(
                    'PostgreSQL remoto requer sslmode=verify-full'
                )

        if (
            not 1 <= self.session_hours <= 24
            or not 1 <= self.query_per_minute <= 60
        ):
            raise ValueError('Limites web inválidos')

    @property
    def secure_cookie(self):
        return self.origin.startswith('https://')

    @classmethod
    def from_env(cls):
        database_url = os.getenv('WEB_DATABASE_URL', '')

        if database_url.startswith('postgresql+psycopg://'):
            import certifi
            from sqlalchemy.engine import make_url

            parsed_url = make_url(database_url)

            database_url = (
                parsed_url
                .update_query_dict({
                    'sslmode': 'verify-full',
                    'sslrootcert': certifi.where()
                })
                .render_as_string(hide_password=False)
            )

        return cls(
            serper_key=os.getenv('SERPER_API_KEY', ''),
            database_url=database_url,
            origin=os.getenv(
                'WEB_ORIGIN',
                'http://127.0.0.1:8000'
            ),
            environment=os.getenv(
                'WEB_ENV',
                'local'
            ),
            external_enabled=(
                os.getenv(
                    'WEB_ENABLE_EXTERNAL',
                    'false'
                ).lower() == 'true'
            )
        )
