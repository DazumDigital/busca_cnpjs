import ssl


def tls_context():
    """TLS verificado sem herdar SSLKEYLOGFILE ou gerar arquivos de chaves TLS."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_default_certs(ssl.Purpose.SERVER_AUTH)
    return context
