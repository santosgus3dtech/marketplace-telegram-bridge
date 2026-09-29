# Referência rápida

## OLX OAuth
Authorization:
`GET https://auth.olx.com.br/oauth`

Parâmetros: `response_type=code`, `client_id`, `redirect_uri`, `scope=chat`, `state`.

Token:
`POST https://auth.olx.com.br/oauth/token`
`Content-Type: application/x-www-form-urlencoded`

Campos: `code`, `client_id`, `client_secret`, `redirect_uri`, `grant_type=authorization_code`.

Retorno documentado: `access_token` e `token_type=Bearer`. A página consultada não documenta refresh token.

## OLX Chat
Registrar/atualizar webhook:
`POST https://apps.olx.com.br/autoservice/v1/chat`
Header `Authorization: Bearer <token>`
Body `{"webhook":"https://..."}`

Desativar:
`DELETE https://apps.olx.com.br/autoservice/v1/chat`

Responder:
`POST https://apps.olx.com.br/autoservice/v1/chat/send`
```json
{
  "textMessage": "Sim, ainda está disponível!",
  "messageId": "...",
  "chatId": "..."
}
```

## Telegram
Base: `https://api.telegram.org/bot<TOKEN>/`
Métodos principais: `sendMessage`, `setWebhook`, `getWebhookInfo`, `deleteWebhook`.
Use `secret_token` e valide `X-Telegram-Bot-Api-Secret-Token`.
Webhooks e `getUpdates` são mutuamente exclusivos.

## Semântica dos webhooks locais
Mensagens válidas de comprador e Replies válidos retornam `200` com status `queued` depois
que mensagem e job foram gravados na mesma transação SQLite. A entrega externa ocorre no
worker persistente e não prolonga a requisição do provedor.

## Cloudflare Tunnel
O `cloudflared` cria conexão outbound-only até a Cloudflare e evita port forwarding.
