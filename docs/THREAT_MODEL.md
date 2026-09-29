# Modelo de ameaças

## Escopo e premissas

O serviço recebe webhooks oficiais da OLX e do Telegram, mantém o mapeamento das conversas
em SQLite e envia mensagens somente pelas APIs oficiais. A implantação suportada usa um
único processo no Raspberry Pi, origem FastAPI vinculada a `127.0.0.1` e Cloudflare Tunnel
sem porta aberta no roteador.

Scraping, automação de login, cookies de sessão, Redis, Kubernetes e brokers externos estão
fora do escopo.

## Ativos protegidos

- token OAuth da OLX e segredo do cliente;
- token do bot e segredo do webhook Telegram;
- senha de app temporária do Gmail;
- chave de criptografia dos tokens;
- conteúdo e mapeamento das conversas;
- nome, email e telefone recebidos no webhook OLX;
- integridade das respostas enviadas à conversa OLX correta;
- disponibilidade do Raspberry, do banco e da fila persistente.

## Fronteiras de confiança

```text
OLX/Telegram -> Cloudflare -> Tunnel -> FastAPI -> SQLite
Gmail IMAP ------------------------------------^ |-> APIs oficiais OLX/Telegram
Operador local -> SSH/Pi Connect -> Docker/arquivos .env e data
```

O header `CF-Connecting-IP` só é confiado quando `TRUST_CLOUDFLARE=true` e a origem continua
inacessível diretamente pela rede pública. Se o serviço for publicado sem Tunnel, essa
confiança deve ser desativada e substituída por uma fronteira de proxy explicitamente
controlada.

## Ameaças e controles

| Ameaça | Impacto | Controles implementados |
|---|---|---|
| Falsificação de webhook OLX | mensagens ou jobs fraudulentos | path aleatório, allowlist do IP documentado, header confiado somente atrás do Tunnel, schema estrito e limite de body |
| Falsificação de webhook Telegram | envio indevido à OLX | `secret_token`, chat e usuário em allowlist, Reply vinculado ao mesmo chat |
| Replay/duplicata | notificações ou respostas repetidas | `messageId` e `update_id` únicos, jobs e mensagens de saída idempotentes |
| Roubo de token no banco | controle da integração OLX | Fernet autenticado; chave somente no `.env`; token nunca registrado em log |
| Sequestro do OAuth | associação a uma autorização indevida | state aleatório, somente hash persistido, TTL, consumo atômico e uso único |
| Abuso ou exaustão HTTP | indisponibilidade e crescimento do banco | limites de payload, timeouts e rate limit em memória com número de chaves limitado |
| Vazamento por documentação ou CORS | descoberta da superfície ou leitura por site hostil | OpenAPI/Swagger/ReDoc desativados em produção e nenhum middleware CORS |
| Vazamento em logs | exposição de PII ou credenciais | logs estruturados sem body, email, telefone, código OAuth ou token; loggers HTTP reduzidos |
| Falha transitória de provedor | perda ou repetição de mensagem | outbox SQLite, lock de propriedade, backoff, limite de tentativas e dead letter |
| Token OLX revogado | respostas silenciosamente perdidas | `401` marca `reauthorization_required`, bloqueia repetição e alerta no Telegram |
| Leitura local do SQLite | exposição de conversas | container não-root, umask `077`, diretório `0700`, banco/sidecars `0600`, raiz do container somente leitura |
| Retenção excessiva de PII | impacto ampliado em incidente | limpeza periódica configurável; remove apenas registros antigos e terminais, preservando jobs ativos |
| Comprometimento do container | alteração do host | capabilities removidas, `no-new-privileges`, root filesystem read-only e único volume gravável em `/app/data` |
| Acesso excessivo ao Gmail | exposição da caixa postal | senha de app exclusiva/revogável, `INBOX` somente leitura, busca limitada e fetch apenas de cabeçalhos |
| Remetente forjado ou confirmação antiga no monitor | alerta falso | domínio exato/subdomínio autorizado, assunto normalizado e corte por `INTERNALDATE`; nenhum link ou corpo é encaminhado |
| Repetição do alerta Gmail | ruído ou confusão | unicidade por UIDVALIDITY/UID, retry durável e encerramento após confirmação no Telegram |

## Retenção

`MESSAGE_RETENTION_DAYS` controla mensagens OLX, atualizações Telegram, respostas, jobs
terminais e chats que ficaram vazios. `AUDIT_RETENTION_DAYS` controla auditoria e states OAuth
expirados. A limpeza roda ao iniciar o worker e depois a cada
`RETENTION_CLEANUP_INTERVAL_SECONDS` quando `RETENTION_CLEANUP_ENABLED=true`.

Jobs pendentes, em processamento ou em retry não são removidos. Registros ainda referenciados
também permanecem, mesmo que já tenham ultrapassado a idade configurada.

## Limitações e riscos residuais

- o rate limit é local ao processo; múltiplas réplicas exigiriam coordenação externa;
- o Raspberry e a conta Cloudflare continuam sendo pontos de confiança administrativos;
- a criptografia do token não protege contra alguém que obtenha simultaneamente banco e
  `.env`;
- o conteúdo precisa ser lido pelo processo para cumprir o fluxo;
- indisponibilidade prolongada da OLX ou Telegram pode produzir dead letters que exigem
  diagnóstico do operador;
- uma alteração do IP oficial de saída da OLX exige atualização explícita da allowlist.
- a senha de app Gmail permite mais acesso à caixa postal do que o monitor utiliza; ela deve
  existir somente durante a espera pela homologação e ser revogada imediatamente depois.

## Resposta a incidentes

1. interromper o container da ponte sem apagar `data`;
2. preservar logs e criar um backup íntegro do SQLite;
3. revogar ou rotacionar o segredo afetado no provedor correspondente;
4. atualizar somente o `.env` local com modo `0600`;
5. reiniciar, executar `scripts/check.sh` e confirmar `/health` e `/ready`;
6. refazer OAuth OLX quando o token ou a chave de criptografia tiver sido comprometido;
7. verificar duplicatas, jobs falhos/dead letter e mensagens enviadas no período.
8. para incidente no monitor, revogar a senha de app Gmail e desativar o worker no `.env`.

Nunca publique tokens, códigos OAuth, `.env`, banco, backups ou URLs contendo segredos em
Git, tickets, capturas de tela ou mensagens.
