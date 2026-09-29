# Segurança

Ameaças consideradas: webhook falso/replay, usuário Telegram indevido, vazamento de tokens, replay OAuth state, duplicatas, resposta à conversa errada e perda do banco.

## Controles obrigatórios
- Telegram secret header + chat/user allowlists + update dedupe;
- OLX path secreto + source IP + messageId dedupe + size limit;
- OAuth state aleatório, hash, TTL e uso único;
- `.env` chmod 600 e ignorado pelo Git;
- token OLX criptografado no DB;
- segredo de criptografia fora do DB;
- replies somente ligados a `telegram_message_id` conhecido;
- mapping de Reply limitado ao mesmo chat Telegram e a mensagens com `origin=buyer`;
- outbound idempotente por `telegram_update_id`, sem registrar token ou corpo de erro da OLX;
- retry limitado somente para timeout/5xx; erros 400/401 são permanentes;
- 401 OLX bloqueia envio e alerta o usuário.
- jobs externos são reclamados com lock exclusivo e só o proprietário pode concluí-los;
- erros persistem apenas códigos internos e status HTTP, nunca tokens ou corpos externos;
- `dead_letter` preserva a evidência sem reenvio infinito.
- monitor Gmail abre a `INBOX` como somente leitura e solicita apenas cabeçalhos;
- senha de app Gmail exclusiva e revogável fica somente no `.env`, nunca no banco/log;
- filtro combina assunto normalizado, domínio exato/subdomínio autorizado e marco temporal;
- alerta Gmail é deduplicado por UIDVALIDITY/UID sem persistir remetente, assunto ou corpo.

## Privacidade
O payload OLX pode conter nome, email e telefone. Não mostrar email/telefone por padrão no
Telegram nem em logs. A retenção ativa usa 90 dias para mensagens e 30 dias para auditoria
por padrão, com configuração pelo ambiente e preservação de jobs ainda ativos.

O monitor temporário envia ao chat privado somente remetente e assunto da resposta encontrada;
o corpo permanece no Gmail. Depois do alerta, revogue a senha de app e desative o monitor.

O inventário completo de ameaças, controles, riscos residuais e resposta a incidentes está
em `docs/THREAT_MODEL.md`.
