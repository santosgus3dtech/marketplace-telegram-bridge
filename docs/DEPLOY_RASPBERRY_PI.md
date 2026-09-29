# Deploy no Raspberry Pi

## Topologia segura

```text
Internet -> Cloudflare Tunnel -> 127.0.0.1:8010 -> container FastAPI:8000
                                             |-> volume ./data:/app/data
```

- Raspberry Pi OS 64-bit/ARM64;
- Docker Engine com plugin Compose;
- nenhuma porta aberta no roteador;
- FastAPI publicado no host somente em `127.0.0.1`;
- `.env` local com modo `600`, nunca versionado;
- hostname público: `bridge.example.com`.

O túnel pode rodar pelo systemd no host **ou** pelo perfil Compose. Não execute as duas
opções ao mesmo tempo para o mesmo túnel.

## 1. Preparar o diretório

```bash
sudo mkdir -p /opt/marketplace-telegram-bridge
sudo chown -R "$USER":"$USER" /opt/marketplace-telegram-bridge
git clone https://github.com/your-account/marketplace-telegram-bridge.git /opt/marketplace-telegram-bridge
cd /opt/marketplace-telegram-bridge
cp .env.example .env
chmod 600 .env
mkdir -p data/backups
```

Preencha o `.env` local. Para o Compose fornecido, mantenha:

```dotenv
APP_BIND_PORT=8010
APP_UID=1000
APP_GID=1000
PUBLIC_BASE_URL=https://bridge.example.com
OLX_REDIRECT_URI=https://bridge.example.com/oauth/olx/callback
DATABASE_URL=sqlite+aiosqlite:////app/data/bridge.db
RATE_LIMIT_ENABLED=true
RETENTION_CLEANUP_ENABLED=true
```

Gere `TOKEN_ENCRYPTION_KEY`, `OLX_WEBHOOK_PATH_SECRET` e `TELEGRAM_WEBHOOK_SECRET`
localmente. Não cole esses valores em Git, logs ou tickets.

## 2. Construir, migrar e iniciar

Em primeira instalação, não há banco anterior para copiar:

```bash
docker compose -f deploy/docker-compose.yml build app
docker compose -f deploy/docker-compose.yml run --rm --no-deps app alembic upgrade head
docker compose -f deploy/docker-compose.yml up -d --no-build app
bash scripts/check.sh
```

Em atualizações, faça backup antes da migration:

```bash
bash scripts/backup-db.sh
git pull --ff-only
docker compose -f deploy/docker-compose.yml build app
docker compose -f deploy/docker-compose.yml run --rm --no-deps app alembic upgrade head
docker compose -f deploy/docker-compose.yml up -d --no-build app
bash scripts/check.sh
```

O container roda como usuário não-root. `APP_UID` e `APP_GID` devem corresponder ao dono do
diretório `data` no host (`id -u` e `id -g` mostram os valores; no Raspberry Pi OS, normalmente
são `1000`). Ele também usa filesystem raiz somente leitura, capabilities removidas,
`no-new-privileges`, healthcheck e `restart: unless-stopped`. O volume `data` é a única área
persistente gravável da aplicação. O entrypoint aplica umask `077`; no primeiro startup, a
aplicação ajusta `data` para `0700` e o SQLite/sidecars para `0600`.

## 3A. Cloudflare Tunnel pelo systemd no host

Instale `cloudflared` pelo pacote oficial compatível com ARM64. Copie
`deploy/cloudflared-config.example.yml` para `/etc/cloudflared/config.yml`, substitua o UUID,
hostname e caminho do arquivo de credenciais e proteja os arquivos:

```bash
sudo install -d -m 700 /etc/cloudflared
sudo chmod 600 /etc/cloudflared/config.yml /etc/cloudflared/SEU_TUNNEL_UUID.json
sudo cloudflared --config /etc/cloudflared/config.yml service install
sudo systemctl enable --now cloudflared
sudo systemctl status cloudflared --no-pager
```

O ingress deve apontar para `http://127.0.0.1:8010`. O último ingress deve ser
`http_status:404`. Não configure port-forwarding no roteador.

## 3B. Cloudflare Tunnel pelo Compose

Esta opção usa um túnel gerenciado remotamente. Grave o token somente no `.env`:

```dotenv
CLOUDFLARE_TUNNEL_TOKEN=valor_fornecido_pela_cloudflare
```

No painel da Cloudflare, configure o hostname público para o serviço privado
`http://app:8000`. Depois inicie o perfil:

```bash
docker compose -f deploy/docker-compose.yml --profile tunnel up -d
docker compose -f deploy/docker-compose.yml --profile tunnel ps
```

O serviço `cloudflared` não publica portas e espera o healthcheck da aplicação.

## 4. Configurar webhooks

Telegram:

```bash
docker compose -f deploy/docker-compose.yml exec -T app \
  python scripts/telegram-set-webhook.py
```

Use `--drop-pending-updates` somente quando a intenção for descartar updates antigos.

O callback OAuth registra o webhook OLX automaticamente. Para registrar novamente usando o
token já criptografado no banco:

```bash
docker compose -f deploy/docker-compose.yml exec -T app \
  python scripts/olx-register-webhook.py
```

Nenhum dos scripts imprime tokens ou o path secreto do webhook OLX.

## 5. Monitor temporário da resposta de homologação

O monitor opcional usa IMAP com TLS em `imap.gmail.com:993`, abre somente a `INBOX` em modo
somente leitura e busca apenas os cabeçalhos `From`, `Subject` e `Message-ID`, além do metadado
`INTERNALDATE` fornecido pelo servidor. O corpo do e-mail não é solicitado. Gere uma senha de
app exclusiva na conta Google com verificação em duas etapas; não use a senha normal da conta.

Prepare os valores não secretos no `.env` do Raspberry (modo `600`) e mantenha o worker
desativado enquanto cria a senha de app:

```bash
python3 -m app.operations.gmail_monitor_config --prepare \
  --username seu-email@gmail.com \
  --not-before 2026-09-29T12:18:35-03:00
```

Defina `GMAIL_REPLY_WATCH_NOT_BEFORE` no instante em que a confirmação automática foi
inspecionada, sempre com offset UTC. Assim, mensagens anteriores ou iguais ao marco são
ignoradas, mas uma resposta nova que chegue durante a configuração ainda será encontrada.

Ative em um terminal interativo. A senha fica oculta enquanto é digitada e não entra no
histórico do shell:

```bash
python3 -m app.operations.gmail_monitor_config --activate
```

Recrie o app e verifique logs sem exibir o `.env`:

```bash
docker compose -f deploy/docker-compose.yml up -d --no-build --force-recreate app
docker compose -f deploy/docker-compose.yml logs --tail 50 app
```

Após o primeiro alerta confirmado no Telegram, o worker para de consultar o Gmail. Remova a
senha de app na Conta Google, apague `GMAIL_IMAP_APP_PASSWORD` do `.env`, defina
`GMAIL_REPLY_WATCH_ENABLED=false` e recrie o app. O utilitário faz as duas alterações sem
exibir a credencial:

```bash
python3 -m app.operations.gmail_monitor_config --disable
```

## 6. Backup e restauração

Backup online consistente, com `PRAGMA integrity_check`:

```bash
bash scripts/backup-db.sh
```

Os arquivos ficam em `data/backups/` com modo `600`. Guarde também uma cópia criptografada
fora do Raspberry. O `.env` deve ser salvo separadamente em cofre seguro; ele não é incluído
no backup do banco.

Restauração exige um arquivo dentro de `data/backups/` e confirmação digitando
`RESTAURAR`:

```bash
bash scripts/restore-db.sh data/backups/bridge-AAAAMMDDTHHMMSSZ.db
```

Antes de substituir o banco, o script cria outro backup `pre-restore`, interrompe o app com
shutdown gracioso, restaura, executa `alembic upgrade head` e reinicia o serviço. Se ocorrer
erro depois da parada, o trap tenta iniciar o app novamente.

## 7. Operação

```bash
bash scripts/check.sh
docker compose -f deploy/docker-compose.yml logs --tail 100 app
docker compose -f deploy/docker-compose.yml restart app
```

Critérios mínimos:

- `health` e `ready` válidos;
- migration no `head`;
- app `healthy` e túnel conectado;
- `/status` responde no Telegram;
- mensagem OLX real chega uma vez;
- Reply do Telegram chega à conversa OLX correta;
- restart preserva banco, mappings e jobs pendentes.
- monitor Gmail, quando ativado, não registra corpo, remetente, assunto ou credencial no banco/log.

## Referências oficiais

- [Cloudflare Tunnel como serviço no Linux](https://developers.cloudflare.com/tunnel/features/locally-managed-tunnels/as-a-service/linux/)
- [Cloudflare Tunnel com Docker](https://developers.cloudflare.com/tunnel/get-started/)
- [IMAP do Gmail](https://developers.google.com/workspace/gmail/imap/imap-smtp)
- [Senhas de app da Conta Google](https://support.google.com/accounts/answer/2461835)
