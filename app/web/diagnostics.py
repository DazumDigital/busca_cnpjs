"""Diagnósticos de banco com vocabulário fechado; nunca imprimir exceções brutas."""
import logging


def database_diagnostic(error):
    original = getattr(error, 'orig', error)
    message = str(original).lower()
    code = getattr(original, 'sqlstate', None)
    if 'certificate' in message or 'sslrootcert' in message:
        return 'DB_CERTIFICATE'
    if 'channel binding' in message:
        return 'DB_CHANNEL_BINDING'
    if code == '28P01' or 'password authentication' in message:
        return 'DB_AUTHENTICATION'
    if code == '42P01' or 'does not exist' in message:
        return 'DB_SCHEMA'
    if 'translate host' in message or 'name resolution' in message:
        return 'DB_DNS'
    if 'timeout' in message or 'timed out' in message:
        return 'DB_TIMEOUT'
    if 'ssl' in message or 'tls' in message:
        return 'DB_TLS'
    return 'DB_OPERATIONAL'


def log_database_error(error):
    logging.getLogger(__name__).error('Banco indisponível: %s', database_diagnostic(error))
