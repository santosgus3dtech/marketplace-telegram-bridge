# Arquitetura completa

## 1. Objetivo
Serviço 24/7 no Raspberry Pi que autentica a conta OLX, recebe mensagens do Chat OLX, encaminha para um chat privado no Telegram e permite responder à conversa correta usando Reply no Telegram.

Fora do escopo: scraping, Selenium/Playwright, automação de senha/login, auto-resposta por IA sem confirmação.

## 2. Componentes
### FastAPI no Raspberry
Endpoints públicos mínimos:
- `GET /oauth/olx/start`
- `GET /oauth/olx/callback`
- `POST /webhooks/olx/{OLX_WEBHOOK_PATH_SECRET}`
- `POST /webhooks/telegram`
- `GET /health`
- `GET /ready`

### Cloudflare Tunnel
Publica o FastAPI por HTTPS sem port-forwarding. O `cloudflared` cria conexão outbound-only.

### OLX
- autorização: `https://auth.olx.com.br/oauth`
- token: `https://auth.olx.com.br/oauth/token`
- registrar webhook: `POST https://apps.olx.com.br/autoservice/v1/chat`
- remover webhook: `DELETE https://apps.olx.com.br/autoservice/v1/chat`
- responder: `POST https://apps.olx.com.br/autoservice/v1/chat/send`
- scope: `chat`

### Telegram
Usar webhook, já que haverá URL HTTPS pública. Configurar `secret_token` no `setWebhook` e validar `X-Telegram-Bot-Api-Secret-Token`.

## 3. OAuth OLX
1. `/oauth/olx/start` gera `state=secrets.token_urlsafe(32)`.
2. Salva somente hash do state, expiração e uso.
3. Redireciona para OAuth OLX com `response_type=code`, `client_id`, `redirect_uri`, `scope=chat`, `state`.
4. OLX retorna `code` e `state`.
5. Callback valida state, TTL e uso único.
6. Faz POST form-urlencoded em `/oauth/token`.
7. Criptografa e persiste `access_token`.
8. Registra webhook do Chat OLX.

O código OLX expira em 10 minutos e não pode ser reutilizado. A documentação consultada não documenta `refresh_token`; não inventar fluxo de refresh. Em 401, exigir nova autorização.

## 4. OLX -> Telegram
Payload documentado inclui `chatId`, `message`, `senderType`, `email`, `name`, `phone`, `messageTimestamp`, `messageId`, `origin`, `listId`.

Passos:
1. validar path secreto e origem;
2. validar schema e tamanho;
3. dedupe por `messageId` UNIQUE;
4. upsert do chat;
5. persistir a mensagem;
6. se `origin=buyer`, criar um `delivery_job` na mesma transação;
7. responder 200 sem depender da disponibilidade do Telegram;
8. o worker chama `sendMessage` e salva o `telegram_message_id` retornado.

Se `origin=seller`, persistir sem gerar nova notificação de comprador, evitando loops.

Mensagem Telegram sugerida:
```text
🟣 Nova mensagem OLX
👤 João
📦 Anúncio ID: 123456789
💬 “Aceita R$ 2.700?”
🆔 Chat: abc123…
```
Email/telefone não devem aparecer por padrão.

## 5. Telegram -> OLX
UX principal: usuário usa Reply sobre a mensagem enviada pelo bot.

1. validar header secreto Telegram;
2. validar `chat.id == TELEGRAM_TARGET_CHAT_ID`;
3. validar `from.id` na allowlist;
4. dedupe por `update_id`;
5. obter `reply_to_message.message_id`;
6. procurar esse ID no mapeamento de mensagem OLX;
7. persistir `outbound_messages` e `delivery_jobs` na mesma transação;
8. responder 200 e deixar o worker chamar `/autoservice/v1/chat/send`;
9. registrar status de saída sem repetir falhas permanentes.

Se não for Reply de uma mensagem mapeada: não enviar e orientar o usuário.

## 6. Banco SQLite
SQLite com WAL. Tabelas sugeridas:

### oauth_states
- id
- state_hash UNIQUE
- created_at
- expires_at
- used_at

### olx_credentials
- id
- access_token_encrypted
- token_type
- connection_status
- created_at/updated_at
- last_401_at

### olx_listings
- list_id UNIQUE
- title
- price NULL, decimal
- status
- created_at/updated_at

Catálogo local para exibir um nome amigável a partir do `listId`. A fonte de cadastro será
definida sem presumir acesso a APIs adicionais além do scope `chat`.

### olx_chats
- chat_id UNIQUE
- list_id
- buyer_name
- buyer_email NULL
- buyer_phone NULL
- last_message_at

### olx_messages
- message_id UNIQUE
- chat_id
- list_id
- origin
- sender_type
- text
- olx_timestamp
- received_at
- telegram_message_id NULL UNIQUE
- telegram_chat_id NULL

### telegram_updates
- update_id UNIQUE
- received_at
- processed_at
- status

### outbound_messages
- telegram_update_id
- olx_chat_id
- olx_reference_message_id
- text
- status
- olx_http_status
- attempts
- created_at/sent_at
- error_code

### audit_events
- event_type
- correlation_id
- metadata_json sem segredos
- created_at

### delivery_jobs
- kind: `olx_to_telegram` ou `telegram_to_olx`
- referência exclusiva à mensagem OLX ou ao outbound
- status: pending/processing/retry/succeeded/failed/dead_letter
- attempts/max_attempts/next_attempt_at
- lock_token/locked_at para impedir processamento simultâneo
- last_error_code sem corpo de resposta ou segredo

## 7. Segurança
### Secrets
`.env` com chmod 600; nunca commitado.

Variáveis críticas:
- OLX_CLIENT_ID
- OLX_CLIENT_SECRET
- OLX_REDIRECT_URI
- TOKEN_ENCRYPTION_KEY
- OLX_WEBHOOK_PATH_SECRET
- TELEGRAM_BOT_TOKEN
- TELEGRAM_WEBHOOK_SECRET
- TELEGRAM_TARGET_CHAT_ID
- TELEGRAM_ALLOWED_USER_IDS
- PUBLIC_BASE_URL

### Webhook OLX
A OLX documenta IP de saída `54.162.151.93`. Atrás do Cloudflare Tunnel, validar `CF-Connecting-IP` somente quando `TRUST_CLOUDFLARE=true` e a origem não estiver exposta diretamente.

Defesa em profundidade:
- Tunnel sem porta aberta;
- path secreto;
- allowlist de IP;
- rate limit;
- schema estrito;
- limite de body;
- logs sem PII/secrets.

### Telegram
- header secret do webhook;
- target chat allowlist;
- user allowlist;
- dedupe por update_id.

### OAuth
- state aleatório;
- hash no DB;
- TTL;
- uso único;
- HTTPS;
- não logar code/token.

### Superfície HTTP
- sem CORS por padrão;
- Swagger, ReDoc e OpenAPI desativados em produção;
- rate limit local e limitado em memória para OAuth e webhooks;
- headers `nosniff`, `no-referrer` e `Permissions-Policy` em todas as respostas;
- CSP restritiva em produção.

## 8. Resiliência
Outbox SQLite + worker asyncio, sem Redis/Celery. O worker reclama cada job com update
atômico e lock de propriedade, retoma jobs pendentes/retry após restart e recupera locks
expirados após o timeout configurado. O shutdown acorda o loop e aguarda a entrega atual.

Retry:
- timeouts/5xx: retry com backoff;
- 400: falha permanente;
- 401: parar, marcar `reauthorization_required`, alertar Telegram.
- limite esgotado: `dead_letter` preservado para diagnóstico.

Retenção:
- limpeza periódica configurável, sem serviço externo;
- remove somente dados expirados e entregas terminais;
- preserva jobs pendentes, em processamento, retry ou ainda referenciados.

## 9. Observabilidade
Logs JSON: timestamp, event, correlation_id, list_id, status, latency. Mascarar chat IDs se desejado e nunca registrar secrets.

Comandos Telegram opcionais:
- `/help`
- `/status`
- `/olx_status`

## 10. Deploy
Raspberry Pi OS 64-bit + Docker/Compose + Cloudflare Tunnel.

```text
Internet -> Cloudflare -> Tunnel -> 127.0.0.1:8000 -> FastAPI -> SQLite
```

Sem port forwarding.
Persistir `./data:/app/data`.

## 11. Critérios de pronto
- OAuth scope chat funciona;
- webhook OLX registra 200/201;
- mensagem real chega uma única vez ao Telegram;
- Reply chega à mesma conversa OLX;
- usuário Telegram não autorizado é ignorado;
- restart mantém mapeamentos;
- duplicatas não geram notificações;
- 401 gera alerta e bloqueia envio;
- `.env` nunca vai para Git;
- testes cobrem state, dedupe, mapping e autorizações.
