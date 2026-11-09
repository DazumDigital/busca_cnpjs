"""Administração local explícita. Nunca recebe senha pela linha de comando."""
import argparse
import getpass
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import select, update, delete

from app.web.auth import create_user, password_hash
from app.web.config import WebSettings
from app.web.migrate import import_consolidation
from app.web.store import Store, users, sessions, consultations, organizations


def create_organization(store, organization, tokens):
    """Cria uma organização isolada (ex.: QA). Nunca altera saldo existente."""
    import re
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{2,79}', organization) or not 0 <= tokens <= 100000:
        raise ValueError('Organização ou quantidade de tokens inválida')
    with store.transaction() as conn:
        created = store.upsert(conn, organizations, dict(id=organization, initial_tokens=tokens, consumed=0, reserved=0), ['id'])
    return 'Organização criada.' if created else 'Organização já existia; saldo preservado sem alteração.'


def main(argv=None):
    cli = argparse.ArgumentParser(description='Administração DazumDigital web; sem pesquisas automáticas')
    cli.add_argument('--env-file', type=Path, default=Path('.env.web'))
    commands = cli.add_subparsers(dest='command', required=True)
    commands.add_parser('init')
    user = commands.add_parser('create-user'); user.add_argument('username'); user.add_argument('--org', default='dazumdigital')
    org = commands.add_parser('create-org'); org.add_argument('organization'); org.add_argument('--tokens', type=int, required=True)
    disable = commands.add_parser('deactivate-user'); disable.add_argument('username')
    reset = commands.add_parser('reset-password'); reset.add_argument('username')
    imp = commands.add_parser('import'); imp.add_argument('source', type=Path)
    external=commands.add_parser('configure-external');external.add_argument('manifest',type=Path);external.add_argument('--legacy-ledger',type=Path)
    commands.add_parser('block-external')
    commands.add_parser('external-status')
    retry = commands.add_parser('retry-job'); retry.add_argument('cnpj'); retry.add_argument('--confirmed-reviewed', action='store_true')
    balance = commands.add_parser('balance'); balance.add_argument('--org', default='dazumdigital')
    worker = commands.add_parser('work-one'); worker.add_argument('--authorize-external', action='store_true')
    cancel = commands.add_parser('cancel-job'); cancel.add_argument('identifier'); cancel.add_argument('--confirmed-worker-stopped', action='store_true')
    args = cli.parse_args(argv)
    load_dotenv(args.env_file, override=False)
    try:
        config = WebSettings.from_env(); store = Store(config.database_url)
        if args.command == 'init':
            store.initialize(); print('Banco web inicializado; saldos existentes preservados.')
        elif args.command == 'create-user':
            first = getpass.getpass('Senha (mínimo 12 caracteres): ')
            if first != getpass.getpass('Repita a senha: '):
                raise ValueError('Senhas diferentes')
            create_user(store, args.username, first, organization=args.org); print('Usuário criado.')
        elif args.command == 'create-org':
            print(create_organization(store, args.organization, args.tokens))
        elif args.command == 'deactivate-user':
            with store.transaction() as conn:
                user_id = conn.execute(select(users.c.id).where(users.c.username == args.username.strip().lower())).scalar_one()
                conn.execute(update(users).where(users.c.id == user_id).values(active=0))
                conn.execute(delete(sessions).where(sessions.c.user_id == user_id))
            print('Usuário desativado; sessões revogadas; consultas e auditoria preservadas.')
        elif args.command == 'block-external':
            from app.web.live import block
            print('Pesquisa externa bloqueada no banco.' if block(store) else 'Nenhuma política externa registrada.')
        elif args.command == 'external-status':
            from app.web.live import external_status
            print(json.dumps(external_status(store), ensure_ascii=False, indent=2))
        elif args.command == 'retry-job':
            if not args.confirmed_reviewed:
                raise ValueError('Revise a auditoria (external-status) e confirme com --confirmed-reviewed')
            from app.web.live import release_job
            from app.services.importer import normalize_cnpj
            print(json.dumps(release_job(store, normalize_cnpj(args.cnpj)), ensure_ascii=False))
        elif args.command == 'reset-password':
            first = getpass.getpass('Nova senha (mínimo 12 caracteres): ')
            if first != getpass.getpass('Repita a senha: '):
                raise ValueError('Senhas diferentes')
            encoded = password_hash(first)
            with store.transaction() as conn:
                user_id = conn.execute(select(users.c.id).where(users.c.username == args.username.lower())).scalar_one()
                conn.execute(update(users).where(users.c.id == user_id).values(password_hash=encoded))
                conn.execute(delete(sessions).where(sessions.c.user_id == user_id))
            print('Senha alterada; sessões anteriores revogadas.')
        elif args.command == 'import':
            print(json.dumps(import_consolidation(store, args.source), ensure_ascii=False))
        elif args.command == 'configure-external':
            from app.web.live import configure
            configure(store,json.loads(args.manifest.read_text(encoding='utf-8')),args.legacy_ledger,key=config.serper_key)
            print('Política externa validada; consumo anterior preservado. Nenhuma chamada externa executada.')
        elif args.command == 'balance':
            print(json.dumps(store.balance(args.org), ensure_ascii=False))
        elif args.command == 'cancel-job':
            if not args.confirmed_worker_stopped:
                raise ValueError('Confirme que o executor parou e revise a auditoria antes de liberar a reserva')
            store.finish_job(args.identifier); print('Reserva liberada se a operação estava pendente. Cotas técnicas preservadas.')
        elif args.command == 'work-one':
            if os.getenv('VERCEL') or not config.external_enabled or not args.authorize_external:
                raise ValueError('Executor apenas local, com WEB_ENABLE_EXTERNAL=true e --authorize-external')
            with store.engine.connect() as conn:
                job = conn.execute(select(consultations).where(
                    consultations.c.organization_id == 'dazumdigital', consultations.c.status == 'NA_FILA')
                    .order_by(consultations.c.created_at).limit(1)).mappings().first()
            if job:
                from app.web.live import process_consultation
                identity = {'id':job['user_id'],'organization_id':job['organization_id']}
                result = process_consultation(store,identity,job['id'],config.serper_key)
                print('Etapa registrada: ' + result['status'])
            else:
                print('Nenhuma consulta aguardando.')
        return 0
    except Exception as error:
        # Do not print database URLs, SQL parameters or provider exception strings.
        print('Operação não concluída. Verifique a configuração. Tipo: ' + type(error).__name__)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
